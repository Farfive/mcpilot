"""Remote multi-user MCP gateway over Streamable HTTP with URL-elicitation login.

Host -> gateway: MCP authorization. The gateway is an OAuth resource server; a
``TokenVerifier`` turns each bearer token into a principal (user + tenant).
Gateway -> providers: per-user MCPilot sessions, a shared encrypted secret store.

Downstream OAuth uses MCP URL elicitation: the host's client shows a link to the
gateway's own ``/connect/<id>`` page, never to the model. That page verifies the
browser's identity (``browser_identity``) before redirecting to the provider and
binds the callback to the same browser with a cookie, so a forwarded login link
cannot attach someone else's account to the initiating user.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import mcp_types as types
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.request_state import RequestStateSecurity
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import UrlElicitationRequiredError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import __version__
from .auth import (
    AuthManager,
    AuthorizationRequest,
    LoginBroker,
    MemorySecretStore,
    OAuthConfig,
    SecretStore,
)
from .catalog import Catalog
from .discovery import CALL_TOOL, DEFINITIONS, FIND_TOOLS, DiscoveryTools
from .installer import PackageInstaller
from .models import Limits
from .policy import Policy
from .runtime import Runtime
from .sdk import AuditSink, MCPilot

logger = logging.getLogger(__name__)
BrowserIdentity = Callable[[Request], Awaitable[str | None]]
_COOKIE = "mcpilot_login"
_MODERN = "2026-07-28"


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant: str = "default"


@dataclass(frozen=True)
class Tenant:
    """One organization's approved catalog and policy."""

    catalog: Catalog
    policy: Policy
    limits: Limits | None = None


def default_principal(token: AccessToken, tenant_claim: str | None = "tenant", user_claim: str | None = None) -> Principal:
    """``issuer|subject`` keeps users of different issuers apart; tenant from a claim.

    ``user_claim`` (e.g. ``email``) uses that claim verbatim instead, so it matches the
    identity an identity-aware proxy injects for ``browser_identity``. Use it only when
    the claim is unique and verified across the configured issuer.
    """
    claims = token.claims or {}
    if user_claim:
        value = claims.get(user_claim)
        if not isinstance(value, str) or not value:
            raise PermissionError("Token lacks the configured user claim")
        user_id = value
    else:
        subject = token.subject or token.client_id
        issuer = claims.get("iss")
        user_id = f"{issuer}|{subject}" if issuer else subject
    tenant = str(claims.get(tenant_claim, "default")) if tenant_claim else "default"
    return Principal(user_id, tenant)


class StaticTokenVerifier:
    """Development and test verifier: fixed tokens mapped to claims (``sub`` required)."""

    def __init__(self, tokens: Mapping[str, Mapping[str, Any]]) -> None:
        self._tokens = {hashlib.sha256(token.encode()).digest(): dict(claims) for token, claims in tokens.items()}

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode()).digest()
        match = next((claims for key, claims in self._tokens.items() if hmac.compare_digest(key, digest)), None)
        if match is None:
            return None
        return AccessToken(token=token, client_id=str(match.get("client_id", "static")),
                           scopes=list(match.get("scopes", [])), subject=str(match["sub"]), claims=match)


class JWTTokenVerifier:
    """JWT access tokens from an organization IdP (JWKS URL or a static JWK set).

    Verifies signature, ``iss``, ``aud`` (the gateway's resource URL), ``exp``
    and ``sub``. Audience validation happens here, so the gateway does not
    rely on ``AccessToken.resource``.
    """

    def __init__(
        self, *, issuer: str, audience: str, jwks_url: str | None = None, jwks: Mapping[str, Any] | None = None,
        algorithms: tuple[str, ...] = ("RS256", "ES256"), leeway: float = 30,
    ) -> None:
        if (jwks_url is None) == (jwks is None):
            raise ValueError("Provide exactly one of jwks_url or jwks")
        self.issuer, self.audience, self.algorithms, self.leeway = issuer, audience, algorithms, leeway
        self._client = jwt.PyJWKClient(jwks_url, cache_keys=True) if jwks_url else None
        self._keys = {key.key_id: key for key in jwt.PyJWKSet.from_dict(dict(jwks)).keys} if jwks else {}

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            if self._client is not None:
                key = (await asyncio.to_thread(self._client.get_signing_key_from_jwt, token)).key
            else:
                key = self._keys[jwt.get_unverified_header(token).get("kid")].key
            claims = jwt.decode(token, key, algorithms=list(self.algorithms), audience=self.audience,
                                issuer=self.issuer, leeway=self.leeway,
                                options={"require": ["exp", "iss", "aud", "sub"]})
        except (jwt.PyJWTError, KeyError, ValueError):
            return None
        scope = claims.get("scope")
        scopes = scope.split() if isinstance(scope, str) else list(claims.get("scp", []))
        return AccessToken(token=token, client_id=str(claims.get("client_id") or claims.get("azp") or "jwt"),
                           scopes=scopes, expires_at=claims.get("exp"), resource=self.audience,
                           subject=str(claims["sub"]), claims=claims)


def trusted_header_identity(header: str) -> BrowserIdentity:
    """Browser identity injected by an identity-aware proxy. Only safe behind that proxy."""

    async def identify(request: Request) -> str | None:
        return request.headers.get(header) or None

    return identify


@dataclass
class _Link:
    id: str
    owner: str
    request: AuthorizationRequest
    expires: float
    used: bool = False

    @property
    def state(self) -> str:
        return parse_qs(urlsplit(self.request.url).query).get("state", [""])[0]


class ConnectLinks:
    """One-time gateway links for pending downstream logins (the LoginBroker UI)."""

    def __init__(self, public_url: str, ttl: float) -> None:
        self.public_url, self.ttl = public_url.rstrip("/"), ttl
        self._links: dict[str, _Link] = {}
        self._changed = asyncio.Event()

    async def notify(self, request: AuthorizationRequest) -> None:
        now = time.monotonic()
        for key, link in tuple(self._links.items()):
            if link.expires < now or (link.owner, link.request.connection_id) == (request.user_id, request.connection_id):
                del self._links[key]
        link = _Link(secrets.token_urlsafe(32), request.user_id, request, now + self.ttl)
        self._links[link.id] = link
        self._changed.set()
        self._changed = asyncio.Event()

    def url(self, link: _Link) -> str:
        return f"{self.public_url}/connect/{link.id}"

    def get(self, link_id: str) -> _Link | None:
        link = self._links.get(link_id)
        return link if link is not None and not link.used and link.expires >= time.monotonic() else None

    def for_user(self, user_id: str) -> list[_Link]:
        return [link for link in self._links.values()
                if link.owner == user_id and self.get(link.id) is not None]

    async def wait(self, user_id: str, integration_id: str, account: str, timeout: float) -> _Link | None:
        deadline = time.monotonic() + timeout
        while True:
            found = next((link for link in self.for_user(user_id)
                          if (link.request.integration_id, link.request.account) == (integration_id, account)), None)
            remaining = deadline - time.monotonic()
            if found is not None or remaining <= 0:
                return found
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)


@dataclass
class _UserSession:
    pilot: MCPilot
    tools: DiscoveryTools
    tenant: str
    last_used: float
    tokens: float
    updated: float
    active: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class GatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RemoteGateway:
    """Multi-user gateway. Run ``app()`` under any ASGI server or call ``serve()``."""

    def __init__(
        self,
        *,
        tenants: Mapping[str, Tenant],
        token_verifier: TokenVerifier,
        public_url: str,
        issuer_url: str,
        state_dir: str | Path,
        secret_store: SecretStore | None = None,
        auth: AuthManager | None = None,
        browser_identity: BrowserIdentity | None = None,
        trust_connect_links: bool = False,
        principal: Callable[[AccessToken], Principal] = default_principal,
        required_scopes: tuple[str, ...] = (),
        request_state_keys: list[bytes] | None = None,
        max_users: int = 1000,
        idle_timeout: float = 900,
        calls_per_minute: int = 120,
        connect_wait: float = 3,
        login_wait: float = 60,
        login_timeout: float = 600,
        audit: AuditSink | None = None,
        name: str = "MCPilot remote gateway",
    ) -> None:
        if not tenants:
            raise ValueError("At least one tenant is required")
        url = urlsplit(public_url)
        if url.scheme not in {"https", "http"} or not url.hostname or url.path not in {"", "/"}:
            raise ValueError("public_url must be an origin such as https://gateway.example.com")
        if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("A remote gateway requires HTTPS outside loopback development")
        self.public_url = public_url.rstrip("/")
        self.tenants = dict(tenants)
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.principal = principal
        self.browser_identity = browser_identity
        self.url_login = browser_identity is not None or trust_connect_links
        self.max_users, self.idle_timeout, self.calls_per_minute = max_users, idle_timeout, calls_per_minute
        self.connect_wait, self.login_wait = connect_wait, login_wait
        self.audit = audit
        self.links = ConnectLinks(self.public_url, login_timeout)
        self.broker = LoginBroker(self.links.notify, timeout=login_timeout)
        self.redirect_uri = f"{self.public_url}/oauth/callback"
        self.auth = auth or AuthManager(secret_store or MemorySecretStore(),
                                        OAuthConfig(login=self.broker, redirect_uri=self.redirect_uri))
        self._installer = PackageInstaller(self.state_dir / "packages")
        self._users: dict[str, _UserSession] = {}
        self._users_lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None
        security = (RequestStateSecurity(keys=request_state_keys) if request_state_keys
                    else RequestStateSecurity.ephemeral())
        self.server = MCPServer(
            name, version=__version__, log_level="CRITICAL", token_verifier=token_verifier,
            auth=AuthSettings(issuer_url=issuer_url, resource_server_url=f"{self.public_url}/mcp",
                              required_scopes=list(required_scopes) or None, validate_token_resource=False),
            request_state_security=security,
        )
        self._register_tools()
        self._register_routes()

    # -- MCP tools -----------------------------------------------------------------------------

    def _register_tools(self) -> None:
        descriptions = {d["name"]: d["description"] for d in DEFINITIONS}

        async def find_tools(task: str, ctx: Context, services: list[str] | None = None
                             ) -> dict[str, Any] | types.InputRequiredResult:
            arguments: dict[str, Any] = {"task": task}
            if services:
                arguments["services"] = services
            return await self._find(ctx, arguments)

        async def call_tool(tool_id: str, arguments: dict[str, Any], ctx: Context) -> dict[str, Any]:
            try:
                async with self._session() as user:
                    return await user.tools.handle(CALL_TOOL, {"tool_id": tool_id, "arguments": arguments})
            except GatewayError as exc:
                return {"error": exc.code, "message": str(exc)}

        self.server.add_tool(find_tools, name=FIND_TOOLS, description=descriptions[FIND_TOOLS])
        self.server.add_tool(call_tool, name=CALL_TOOL, description=descriptions[CALL_TOOL])

    async def _find(self, ctx: Context, arguments: dict[str, Any]) -> dict[str, Any] | types.InputRequiredResult:
        try:
            async with self._session() as user:
                state = json.loads(ctx.request_state) if ctx.request_state else None
                if state is not None:
                    # Retry after the client opened the login links: wait for accepted logins.
                    responses = ctx.input_responses or {}
                    accepted = [tuple(key) for index, key in enumerate(state.get("logins", []))
                                if getattr(responses.get(f"login_{index}"), "action", None) == "accept"]
                    await user.tools.wait_pending(accepted, self.login_wait)
                result = await user.tools.handle(FIND_TOOLS, arguments)
                logins = [] if state is not None else await self._pending_links(user, result)
                if logins and self._supports_url_elicitation(ctx):
                    return self._elicit(ctx, logins)
                if result.get("needs_user") and self.browser_identity is not None:
                    result["connect_page"] = f"{self.public_url}/connect"  # static page, no secrets
                return result
        except GatewayError as exc:
            return {"error": exc.code, "message": str(exc)}

    async def _pending_links(self, user: _UserSession, result: dict[str, Any]) -> list[_Link]:
        if not self.url_login:
            return []
        links = []
        for need in result.get("needs_user", []):
            if need["action"] == "finish_login":
                link = await self.links.wait(user.pilot.user_id, need["integration_id"], need["account"],
                                             self.connect_wait)
                if link is not None:
                    links.append(link)
        return links

    @staticmethod
    def _supports_url_elicitation(ctx: Context) -> bool:
        caps = ctx.client_capabilities
        return bool(caps and caps.elicitation and caps.elicitation.url)

    def _elicit(self, ctx: Context, links: list[_Link]) -> types.InputRequiredResult:
        messages = {link.id: f"Połącz konto {link.request.integration_id} ({link.request.account}), "
                             "aby kontynuować zadanie." for link in links}
        if (ctx.protocol_version or "") < _MODERN:
            raise UrlElicitationRequiredError([
                types.ElicitRequestURLParams(message=messages[link.id], url=self.links.url(link), elicitation_id=link.id)
                for link in links])
        return types.InputRequiredResult(
            input_requests={f"login_{index}": types.ElicitRequest(
                method="elicitation/create",
                params=types.ElicitRequestURLParams(message=messages[link.id], url=self.links.url(link)),
            ) for index, link in enumerate(links)},
            request_state=json.dumps({"v": 1, "logins": [[link.request.integration_id, link.request.account]
                                                          for link in links]}),
        )

    # -- per-user sessions ---------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _session(self):
        token = get_access_token()
        if token is None:
            raise GatewayError("Unauthenticated", "Gateway requires an authenticated MCP session")
        try:
            principal = self.principal(token)
        except PermissionError:
            raise GatewayError("PolicyDenied", "The access token does not identify a gateway user") from None
        tenant = self.tenants.get(principal.tenant)
        if tenant is None:
            raise GatewayError("PolicyDenied", "The user's organization has no gateway configuration")
        user = await self._user(principal, tenant)
        now = time.monotonic()
        user.tokens = min(self.calls_per_minute, user.tokens + (now - user.updated) * self.calls_per_minute / 60)
        user.updated = user.last_used = now
        if user.tokens < 1:
            raise GatewayError("RateLimited", "Too many gateway calls; retry later")
        user.tokens -= 1
        user.active += 1
        try:
            yield user
        finally:
            user.active -= 1
            user.last_used = time.monotonic()

    async def _user(self, principal: Principal, tenant: Tenant) -> _UserSession:
        async with self._users_lock:
            if self._sweeper is None or self._sweeper.done():
                self._sweeper = asyncio.create_task(self._sweep(), name="mcpilot-gateway-sweeper")
            user = self._users.get(principal.user_id)
            if user is not None and user.tenant != principal.tenant:
                await self._close_user(principal.user_id)  # policy changed with the organization
                user = None
            if user is None:
                if len(self._users) >= self.max_users and not await self._evict(force=True):
                    raise GatewayError("Capacity", "Gateway is at capacity; retry later")
                digest = hashlib.sha256(principal.user_id.encode()).hexdigest()[:32]
                runtime = Runtime(self.state_dir / "users" / digest / "runtimes")
                runtime.installer = self._installer  # share pinned packages, not processes
                pilot = MCPilot(user_id=principal.user_id, catalog=tenant.catalog, policy=tenant.policy,
                                auth=self.auth, runtime=runtime, limits=tenant.limits, audit=self.audit,
                                tenant=principal.tenant)
                now = time.monotonic()
                async def login_started(integration_id: str, account: str, user_id: str = principal.user_id) -> None:
                    if await self.links.wait(user_id, integration_id, account, self.connect_wait) is None:
                        await asyncio.Event().wait()  # no link: let the connection timeout decide

                tools = DiscoveryTools(pilot, connect_wait=self.connect_wait,
                                       login_started=login_started if self.url_login else None)
                user = _UserSession(pilot, tools, principal.tenant, now, float(self.calls_per_minute), now)
                self._users[principal.user_id] = user
            return user

    async def _close_user(self, user_id: str) -> None:
        user = self._users.pop(user_id, None)
        if user is not None:
            await user.tools.close()
            await user.pilot.close()

    async def _evict(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        idle = sorted((u.last_used, key) for key, u in self._users.items()
                      if u.active == 0 and (force or now - u.last_used > self.idle_timeout))
        for _, key in idle[:1] if force else idle:
            await self._close_user(key)
        return bool(idle)

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, max(self.idle_timeout / 4, 0.05)))
            async with self._users_lock:
                await self._evict()

    def active_users(self) -> int:
        return len(self._users)

    # -- browser routes ------------------------------------------------------------------------

    def _register_routes(self) -> None:
        secure = self.public_url.startswith("https://")

        def page(title: str, body: str, status: int = 200) -> HTMLResponse:
            return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
                                f"<h1>{html.escape(title)}</h1>{body}", status_code=status,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

        async def identify(request: Request) -> str | None:
            return await self.browser_identity(request) if self.browser_identity is not None else None

        @self.server.custom_route("/healthz", methods=["GET"])
        async def health(_: Request) -> Response:
            return JSONResponse({"status": "ok", "users": self.active_users()})

        @self.server.custom_route("/connect", methods=["GET"])
        async def pending(request: Request) -> Response:
            user = await identify(request)
            if user is None:
                return page("Wymagane logowanie", "<p>Zaloguj się do bramy w organizacji.</p>", 401)
            items = "".join(f'<li><a href="/connect/{link.id}">{html.escape(link.request.integration_id)} '
                            f'({html.escape(link.request.account)})</a></li>' for link in self.links.for_user(user))
            return page("Połączenia do potwierdzenia", f"<ul>{items}</ul>" if items else "<p>Brak.</p>")

        @self.server.custom_route("/connect/{link_id}", methods=["GET"])
        async def connect(request: Request) -> Response:
            link = self.links.get(request.path_params["link_id"])
            if link is None or not self.url_login:
                return page("Link wygasł", "<p>Wróć do aplikacji i spróbuj ponownie.</p>", 404)
            if self.browser_identity is not None and await identify(request) != link.owner:
                # A forwarded link must not attach this browser's account to someone else.
                return page("Inny użytkownik", "<p>Ten link logowania należy do innego konta.</p>", 403)
            response = RedirectResponse(link.request.url, status_code=302)
            response.set_cookie(_COOKIE, link.id, max_age=int(self.links.ttl), path="/oauth/callback",
                                httponly=True, secure=secure, samesite="lax")
            return response

        @self.server.custom_route("/oauth/callback", methods=["GET"])
        async def callback(request: Request) -> Response:
            link = self.links.get(request.cookies.get(_COOKIE, ""))
            state = request.query_params.get("state", "")
            if link is None or not state or not hmac.compare_digest(link.state, state):
                return page("Nieznane logowanie", "<p>Otwórz link logowania z aplikacji w tej przeglądarce.</p>", 400)
            link.used = True
            accepted = self.broker.complete(
                user_id=link.owner, state=state, code=request.query_params.get("code"),
                iss=request.query_params.get("iss"), error=request.query_params.get("error"))
            response = (page("Konto połączone", "<p>Możesz zamknąć tę kartę i wrócić do aplikacji.</p>") if accepted
                        else page("Logowanie wygasło", "<p>Wróć do aplikacji i spróbuj ponownie.</p>", 400))
            response.delete_cookie(_COOKIE, path="/oauth/callback")
            return response

    # -- serving -------------------------------------------------------------------------------

    def app(self):
        host = urlsplit(self.public_url).netloc
        return self.server.streamable_http_app(
            host="0.0.0.0",
            transport_security=TransportSecuritySettings(allowed_hosts=[host], allowed_origins=[self.public_url]),
        )

    async def serve(self, host: str = "127.0.0.1", port: int = 8000, *, sockets: list | None = None) -> None:
        import uvicorn

        server = uvicorn.Server(uvicorn.Config(self.app(), host=host, port=port, log_level="warning"))
        try:
            await server.serve(sockets=sockets)
        finally:
            await self.close()

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        async with self._users_lock:
            for key in tuple(self._users):
                await self._close_user(key)
