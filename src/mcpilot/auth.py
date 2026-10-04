"""Host-owned credentials and policy-bounded MCP OAuth.

Nothing in this module opens a browser, reads environment secrets, or creates
files at import time. OAuth is driven by the official MCP Python SDK 2.3.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import httpx2
from cryptography.fernet import Fernet, InvalidToken
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.auth.exceptions import OAuthRegistrationError
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

from .canonical import canonical_json, sha256_hex


class AuthFailure(RuntimeError):
    """A deliberately credential-free authentication failure."""


class AuthRequired(AuthFailure):
    """The host must connect the account or obtain broader consent."""

    status = "auth_required"


class AuthSetupRequired(AuthFailure):
    """The host must configure a provider application or administrator consent."""

    status = "setup_required"


@dataclass(frozen=True)
class CredentialKey:
    user_id: str
    integration_id: str
    account: str
    endpoint: str

    @property
    def digest(self) -> str:
        # RFC 8785 canonical form: the TypeScript SDK derives the same key and connection id.
        return sha256_hex(canonical_json([self.user_id, self.integration_id, self.account, self.endpoint]))


class SecretStore(Protocol):
    """Implement these operations using an application secret manager if needed."""

    async def get(self, key: CredentialKey) -> dict[str, Any] | None: ...
    async def set(self, key: CredentialKey, value: Mapping[str, Any]) -> None: ...
    async def delete(self, key: CredentialKey) -> None: ...


class MemorySecretStore:
    def __init__(self) -> None:
        self._values: dict[str, dict[str, Any]] = {}

    async def get(self, key: CredentialKey) -> dict[str, Any] | None:
        return copy.deepcopy(self._values.get(key.digest))

    async def set(self, key: CredentialKey, value: Mapping[str, Any]) -> None:
        self._values[key.digest] = copy.deepcopy(dict(value))

    async def delete(self, key: CredentialKey) -> None:
        self._values.pop(key.digest, None)


class EncryptedFileSecretStore:
    """Atomic encrypted records, 0600 files, external Fernet key.

    Intended for one application process. Distributed applications should supply
    a transactional SecretStore backed by their existing secret manager.
    """

    def __init__(self, directory: str | Path, encryption_key: bytes) -> None:
        self._directory = Path(directory)
        self._cipher = Fernet(encryption_key)
        self._lock = asyncio.Lock()

    async def get(self, key: CredentialKey) -> dict[str, Any] | None:
        async with self._lock:
            path = self._directory / f"{key.digest}.enc"
            try:
                encrypted = path.read_bytes()
            except FileNotFoundError:
                return None
            try:
                return json.loads(self._cipher.decrypt(encrypted))
            except (InvalidToken, ValueError, UnicodeError):
                raise AuthFailure("Credential storage could not be decrypted.") from None

    async def set(self, key: CredentialKey, value: Mapping[str, Any]) -> None:
        encrypted = self._cipher.encrypt(json.dumps(dict(value)).encode())
        async with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".credential-", dir=self._directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encrypted)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self._directory / f"{key.digest}.enc")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    async def delete(self, key: CredentialKey) -> None:
        async with self._lock:
            (self._directory / f"{key.digest}.enc").unlink(missing_ok=True)


CallbackResult = AuthorizationCodeResult | tuple[str, str] | tuple[str, str, str]


@dataclass(frozen=True)
class AuthorizationRequest:
    """Host UI event. Never place its URL in an LLM prompt or audit payload."""

    connection_id: str
    integration_id: str
    account: str
    scopes: tuple[str, ...]
    url: str = field(repr=False)
    user_id: str = ""


@dataclass
class _PendingLogin:
    request: AuthorizationRequest
    future: asyncio.Future[AuthorizationCodeResult]


class LoginBroker:
    """Turn the in-flow OAuth wait into a login button and a host callback route.

    ``notify`` receives an ``AuthorizationRequest`` for the authenticated user's
    UI: render ``request.url`` as a "Connect" button and never send it to a model.
    The host redirect route calls ``complete``/``complete_url``. The waiting
    connection then continues in the task that started it. An unanswered login
    ends as ``AuthRequired`` after ``timeout`` and a persisted workflow can be
    resumed later. PKCE verifiers stay in process memory, so a restart during a
    login requires a new click rather than restoring a half-finished flow.
    """

    def __init__(
        self,
        notify: Callable[[AuthorizationRequest], Awaitable[None]] | None = None,
        *,
        timeout: float = 600.0,
        max_pending: int = 1000,
    ) -> None:
        if timeout <= 0 or max_pending < 1:
            raise ValueError("Login timeout and pending limit must be positive")
        self._notify = notify
        self.timeout = timeout
        self.max_pending = max_pending
        self._pending: dict[str, _PendingLogin] = {}

    @staticmethod
    def _state(url: str) -> str:
        state = parse_qs(urlparse(url).query).get("state", [""])[0]
        if not state:
            raise AuthFailure("Authorization URL has no state parameter.")
        return state

    def pending(self, user_id: str) -> tuple[AuthorizationRequest, ...]:
        """Open login buttons for one host-authenticated user (e.g. after a page reload)."""
        return tuple(item.request for item in self._pending.values()
                     if item.request.user_id == user_id and not item.future.done())

    async def begin(self, request: AuthorizationRequest) -> None:
        state = self._state(request.url)
        for key, item in tuple(self._pending.items()):
            if (item.request.user_id, item.request.connection_id) == (request.user_id, request.connection_id):
                # A newer attempt for the same connection supersedes the old button.
                if not item.future.done():
                    item.future.set_exception(AuthRequired("Login attempt was superseded."))
                del self._pending[key]
        if len(self._pending) >= self.max_pending:
            raise AuthRequired("Too many pending logins; try again later.")
        self._pending[state] = _PendingLogin(request, asyncio.get_running_loop().create_future())
        if self._notify is not None:
            try:
                await self._notify(request)
            except BaseException:
                # No button reached the user; do not keep an unreachable pending login.
                self._pending.pop(state, None)
                raise

    async def wait(self, request: AuthorizationRequest) -> AuthorizationCodeResult:
        state = self._state(request.url)
        item = self._pending.get(state)
        if item is None:
            raise AuthRequired("Login was not started; reconnect the account.")
        try:
            async with asyncio.timeout(self.timeout):
                return await item.future
        except TimeoutError:
            raise AuthRequired("Login was not completed in time; resume after connecting.") from None
        finally:
            if self._pending.get(state) is item:
                del self._pending[state]

    def complete(
        self, *, user_id: str, state: str, code: str | None = None,
        iss: str | None = None, error: str | None = None,
    ) -> bool:
        """Deliver the provider redirect. Returns False for unknown state or another user.

        Safe to call from another thread; the result is applied on the loop
        that is waiting for the login.
        """
        item = self._pending.get(state)
        if item is None or item.future.done() or item.request.user_id != user_id:
            return False

        def resolve() -> None:
            if item.future.done():
                return
            if error or not code:
                item.future.set_exception(AuthRequired("The user did not grant access."))
            else:
                item.future.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))

        loop = item.future.get_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            resolve()
        else:
            loop.call_soon_threadsafe(resolve)
        return True

    def complete_url(self, *, user_id: str, callback_url: str) -> bool:
        """Convenience for a host redirect route: pass the full callback URL."""
        params = parse_qs(urlparse(callback_url).query)

        def first(name: str) -> str | None:
            values = params.get(name)
            return values[0] if values else None

        return self.complete(user_id=user_id, state=first("state") or "", code=first("code"),
                             iss=first("iss"), error=first("error"))

    def cancel(self, user_id: str, connection_id: str) -> bool:
        for item in tuple(self._pending.values()):
            request = item.request
            if (request.user_id, request.connection_id) == (user_id, connection_id) and not item.future.done():
                item.future.set_exception(AuthRequired("The user cancelled the login."))
                return True
        return False


@dataclass(frozen=True)
class OAuthConfig:
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    client_id: str | None = field(default=None, repr=False)
    client_secret: str | None = field(default=None, repr=False)
    issuer: str | None = None
    client_metadata_url: str | None = None
    scopes: tuple[str, ...] = ()
    redirect_handler: Callable[[str], Awaitable[None]] | None = field(default=None, repr=False)
    callback_handler: Callable[[], Awaitable[CallbackResult]] | None = field(default=None, repr=False)
    authorization_handler: Callable[[AuthorizationRequest], Awaitable[None]] | None = field(default=None, repr=False)
    allowed_auth_hosts: tuple[str, ...] = ()
    token_endpoint_auth_method: str = "none"
    login: LoginBroker | None = field(default=None, repr=False)
    authorization_timeout: float = 120.0


@dataclass
class AuthHandle:
    connection_id: str
    status: str
    capabilities: tuple[str, ...]
    reason: str | None = None
    http_auth: httpx2.Auth | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    # Interactive authorization runs before the MCP session, outside its timeouts.
    authorize: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    def public_state(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "status": self.status,
            "capabilities": list(self.capabilities) if self.status == "ready" else [],
            "reason": self.reason,
        }


class _ScopedTokenStorage:
    def __init__(self, store: SecretStore, key: CredentialKey, scopes: tuple[str, ...]) -> None:
        self.store, self.key, self.scopes = store, key, scopes
        self.provider: _PolicyOAuthProvider | None = None

    async def get_tokens(self) -> OAuthToken | None:
        record = await self.store.get(self.key) or {}
        raw = record.get("tokens")
        if raw is None:
            return None
        token = OAuthToken.model_validate(raw)
        if not set(self.scopes).issubset(set((token.scope or "").split())):
            return None
        expires_at = record.get("expires_at")
        if expires_at is not None:
            token.expires_in = int(expires_at - time.time())
        return token

    async def set_tokens(self, tokens: OAuthToken) -> None:
        if not set(self.scopes).issubset(set((tokens.scope or "").split())):
            raise AuthRequired("Provider did not grant the requested capabilities.")
        record = await self.store.get(self.key) or {}
        record["tokens"] = tokens.model_dump(mode="json")
        record["expires_at"] = None if tokens.expires_in is None else time.time() + tokens.expires_in
        if self.provider is not None and self.provider.context.oauth_metadata is not None:
            record["oauth_metadata"] = self.provider.context.oauth_metadata.model_dump(mode="json")
        await self.store.set(self.key, record)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        record = await self.store.get(self.key) or {}
        raw = record.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw is not None else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        record = await self.store.get(self.key) or {}
        record["client_info"] = client_info.model_dump(mode="json")
        await self.store.set(self.key, record)


class _PolicyOAuthProvider(OAuthClientProvider):
    """Two small, tested adaptations to the pinned SDK 2.3 provider.

    The SDK otherwise requests all scopes advertised by metadata and does not
    restore token expiry / discovered token endpoint when loading TokenStorage.
    """

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        assert isinstance(storage, _ScopedTokenStorage)
        record = await storage.store.get(storage.key) or {}
        if record.get("oauth_metadata"):
            self.context.oauth_metadata = OAuthMetadata.model_validate(record["oauth_metadata"])
            self.context.auth_server_url = str(self.context.oauth_metadata.issuer)
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)

    async def _perform_authorization(self) -> httpx2.Request:
        storage = self.context.storage
        assert isinstance(storage, _ScopedTokenStorage)
        self.context.client_metadata.scope = " ".join(storage.scopes) or None
        return await super()._perform_authorization()


_oauth_active: ContextVar[bool] = ContextVar("mcpilot_oauth_active", default=False)


class _OAuthLogRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _oauth_active.get():
            record.msg, record.args = "MCP OAuth diagnostic (sensitive detail omitted)", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def _install_log_redactor() -> None:
    logger = logging.getLogger("mcp.client.auth.oauth2")
    if not any(isinstance(item, _OAuthLogRedactor) for item in logger.filters):
        logger.addFilter(_OAuthLogRedactor())


class _SafeOAuthAuth(httpx2.Auth):
    def __init__(self, provider: _PolicyOAuthProvider, scopes: tuple[str, ...], config: OAuthConfig) -> None:
        self._provider, self._scopes, self._config = provider, scopes, config

    async def async_auth_flow(self, request: httpx2.Request):
        flow = self._provider.async_auth_flow(request)
        try:
            outgoing = await self._advance(flow, None)
            while True:
                self._validate_destination(outgoing)
                # Registration metadata must not ask for all advertised scopes.
                if outgoing.method == "POST" and outgoing.headers.get("content-type", "").startswith("application/json"):
                    body = json.loads(outgoing.content)
                    if "redirect_uris" in body:
                        body["scope"] = " ".join(self._scopes) or None
                        outgoing = httpx2.Request(outgoing.method, outgoing.url, json=body,
                            headers={k: v for k, v in outgoing.headers.items() if k != "content-length"})
                response = yield outgoing
                if response.status_code in (401, 403):
                    from mcp.client.auth.utils import extract_scope_from_www_auth
                    challenge = extract_scope_from_www_auth(response)
                    if challenge and not set(challenge.split()).issubset(self._scopes):
                        raise AuthRequired("Server requires additional scopes; update policy and reconnect.")
                outgoing = await self._advance(flow, response)
        except StopAsyncIteration:
            return
        except AuthFailure:
            raise
        except OAuthRegistrationError:
            raise AuthSetupRequired("OAuth client registration requires host configuration.") from None
        except Exception:
            raise AuthFailure("OAuth authorization failed; reconnect the account through the host UI.") from None
        finally:
            await flow.aclose()

    async def _advance(self, flow: Any, response: httpx2.Response | None):
        marker = _oauth_active.set(True)
        try:
            return await flow.__anext__() if response is None else await flow.asend(response)
        finally:
            _oauth_active.reset(marker)

    def _validate_destination(self, request: httpx2.Request) -> None:
        target = urlparse(str(request.url))
        endpoint = urlparse(self._provider.context.server_url)
        loopback = {"127.0.0.1", "localhost", "::1"}
        if target.scheme != "https" and not (
            target.scheme == "http" and target.hostname in loopback and endpoint.hostname in loopback
        ):
            raise AuthFailure("OAuth requires HTTPS except for explicit loopback development endpoints.")
        if self._config.allowed_auth_hosts and target.hostname not in {
            endpoint.hostname, *self._config.allowed_auth_hosts
        }:
            raise AuthFailure("OAuth discovery requested an unapproved authorization host.")


async def _authorization_probe(endpoint: str, auth: httpx2.Auth, limit: float) -> None:
    """Complete discovery, login, refresh or re-consent before opening an MCP session.

    A user may need minutes to click a login button, while MCP initialization
    has short timeouts. The probe is a JSON-RPC ``ping``; any status other than
    401/403 proves the transport accepted the credential. The body is not read.
    """
    try:
        async with httpx2.AsyncClient(auth=auth, follow_redirects=False, trust_env=False, timeout=30) as client:
            async with asyncio.timeout(limit):
                async with client.stream(
                    "POST", endpoint,
                    json={"jsonrpc": "2.0", "id": "mcpilot-authorization-probe", "method": "ping"},
                    headers={"Accept": "application/json, text/event-stream"},
                ) as response:
                    status = response.status_code
    except AuthFailure:
        raise
    except TimeoutError:
        raise AuthRequired("Authorization did not finish in time; reconnect the account.") from None
    except Exception:
        raise AuthFailure("Authorization endpoint is unavailable.") from None
    if status in (401, 403):
        raise AuthRequired("The MCP server did not accept the account authorization.")


class AuthManager:
    def __init__(self, store: SecretStore | None = None, oauth_config: OAuthConfig | None = None) -> None:
        self.store = store if store is not None else MemorySecretStore()
        self._default_oauth = oauth_config
        self._oauth_configs: dict[str, OAuthConfig] = {}
        self._providers: dict[str, _SafeOAuthAuth] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def configure_oauth(self, integration_id: str, config: OAuthConfig) -> None:
        self._oauth_configs[integration_id] = config

    async def set_secret(
        self, user_id: str, integration_id: str, account: str, secret: str, *, endpoint: str
    ) -> None:
        if not secret or "\r" in secret or "\n" in secret:
            raise AuthFailure("A nonempty single-line credential is required.")
        await self.store.set(CredentialKey(user_id, integration_id, account, endpoint), {"secret": secret})

    async def prepare(
        self, user_id: str, integration: Any, account: str, capabilities: tuple[str, ...],
        oauth_config: OAuthConfig | None = None,
    ) -> AuthHandle:
        key = CredentialKey(user_id, integration.id, account, integration.endpoint or "stdio")
        connection_id = f"conn_{key.digest[:24]}"
        capabilities = tuple(sorted(set(capabilities)))
        spec = integration.auth
        handle = AuthHandle(connection_id, "ready", capabilities)
        if spec.mode == "none":
            return handle
        record = await self.store.get(key) or {}
        if spec.mode in ("bearer", "api_key"):
            secret = record.get("secret")
            if not secret:
                handle.status = "setup_required" if spec.setup_required else "auth_required"
                handle.reason = "Configure the provider application." if spec.setup_required else "Connect the account through the host UI."
                return handle
            if integration.transport == "stdio":
                if not spec.env_var:
                    return AuthHandle(connection_id, "setup_required", (), "Configure the credential environment variable.")
                handle.env[spec.env_var] = secret
            else:
                handle.headers[spec.header_name] = f"Bearer {secret}" if spec.mode == "bearer" else secret
            return handle
        if spec.mode != "oauth" or integration.transport != "http":
            return AuthHandle(connection_id, "setup_required", (), "This authentication adapter is not supported.")
        config = oauth_config or self._oauth_configs.get(integration.id) or self._default_oauth
        if config is None:
            if spec.setup_required:
                return AuthHandle(connection_id, "setup_required", (), "Configure the provider OAuth application.")
            # Stored grants remain usable without a login UI; a new login is not.
            config = OAuthConfig()
        if config.client_secret and not config.client_id:
            return AuthHandle(connection_id, "setup_required", (), "OAuth client configuration is incomplete.")
        scopes = tuple(sorted({scope for cap in capabilities for scope in spec.scopes_by_capability.get(cap, ())}
                              | set(config.scopes)))
        storage = _ScopedTokenStorage(self.store, key, scopes)
        stored = await storage.get_tokens()
        has_ui = config.login is not None or (
            (config.redirect_handler is not None or config.authorization_handler is not None)
            and config.callback_handler is not None
        )
        if stored is None and not has_ui:
            return AuthHandle(connection_id, "auth_required", (), "Connect the account through the host UI.")
        current: AuthorizationRequest | None = None

        async def redirect(url: str) -> None:
            nonlocal current
            if not has_ui:
                raise AuthRequired("Reconnect the account through the host UI.")
            requested = set(parse_qs(urlparse(url).query).get("scope", [""])[0].split())
            if not requested.issubset(scopes):
                raise AuthRequired("Additional authorization requires a policy update.")
            current = AuthorizationRequest(connection_id, integration.id, account, scopes, url, user_id)
            if config.login is not None:
                await config.login.begin(current)
            elif config.authorization_handler is not None:
                await config.authorization_handler(current)
            elif config.redirect_handler is not None:
                await config.redirect_handler(url)

        async def callback() -> AuthorizationCodeResult:
            if config.login is not None and current is not None:
                return await config.login.wait(current)
            if config.callback_handler is None:
                raise AuthRequired("Reconnect the account through the host UI.")
            result = await config.callback_handler()
            if isinstance(result, AuthorizationCodeResult):
                return result
            return AuthorizationCodeResult(code=result[0], state=result[1], iss=result[2] if len(result) > 2 else None)

        try:
            metadata = OAuthClientMetadata(
                client_name="MCPilot", redirect_uris=[AnyUrl(config.redirect_uri)],
                scope=" ".join(scopes) or None, token_endpoint_auth_method=config.token_endpoint_auth_method,
            )
            if config.client_id and await storage.get_client_info() is None:
                if not config.issuer:
                    return AuthHandle(connection_id, "setup_required", (), "Bind preregistered OAuth credentials to their issuer.")
                info = OAuthClientInformationFull(
                    **metadata.model_dump(), client_id=config.client_id, client_secret=config.client_secret, issuer=config.issuer
                )
                await storage.set_client_info(info)
            provider = _PolicyOAuthProvider(
                server_url=integration.endpoint, client_metadata=metadata, storage=storage,
                redirect_handler=redirect, callback_handler=callback, client_metadata_url=config.client_metadata_url,
            )
        except Exception:
            return AuthHandle(connection_id, "setup_required", (), "OAuth configuration is invalid.")
        storage.provider = provider
        provider.context.lock = self._locks.setdefault(key.digest, asyncio.Lock())
        _install_log_redactor()
        auth = _SafeOAuthAuth(provider, scopes, config)
        handle.http_auth = auth
        self._providers[key.digest] = auth
        limit = config.authorization_timeout + (config.login.timeout if config.login is not None else 0)

        async def authorize() -> None:
            await _authorization_probe(integration.endpoint, auth, limit)

        handle.authorize = authorize
        return handle

    async def revoke(
        self, user_id: str, integration_id: str, account: str, *, endpoint: str,
        provider_revoker: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Forget local grants; optionally revoke at the provider through a host adapter.

        The host must first disconnect any active runtime session. A detached
        copy of a bearer token cannot be revoked locally.
        """
        key = CredentialKey(user_id, integration_id, account, endpoint or "stdio")
        record = await self.store.get(key) or {}
        provider = self._providers.pop(key.digest, None)
        if provider is not None:
            provider._provider.context.clear_tokens()
        await self.store.delete(key)
        token = record.get("tokens", {}).get("refresh_token") or record.get("tokens", {}).get("access_token") or record.get("secret")
        if token and provider_revoker is not None:
            try:
                await provider_revoker(token)
            except Exception:
                raise AuthFailure("Local access was removed; provider revocation did not complete.") from None
