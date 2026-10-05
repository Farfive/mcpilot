"""Optional MCP gateway: other MCP hosts reach MCPilot through one connection.

Run: python -m mcpilot.gateway --config gateway.json

A configuration with ``"transport": "http"`` starts the multi-user remote
gateway (``mcpilot.remote``). Otherwise the gateway serves one host-configured
user over stdio. Its tools are the
discovery meta-tools, so the host's model never receives the full catalog,
endpoints, commands or credentials. Downstream OAuth opens the user's browser
and returns to a loopback callback; login URLs never enter tool results.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from . import __version__
from .audit import JsonlAuditSink
from .auth import (
    AuthManager,
    AuthorizationRequest,
    EncryptedFileSecretStore,
    LoginBroker,
    MemorySecretStore,
    OAuthConfig,
    SecretStore,
)
from .catalog import Catalog
from .discovery import CALL_TOOL, DEFINITIONS, FIND_TOOLS, DiscoveryTools
from .keys import resolve_key
from .policy import Policy
from .sdk import MCPilot
from .search import CapabilityIndex


def create_gateway(tools: DiscoveryTools, *, name: str = "MCPilot gateway") -> MCPServer:
    server = MCPServer(name, version=__version__, log_level="CRITICAL")
    descriptions = {d["name"]: d["description"] for d in DEFINITIONS}

    async def find_tools(task: str, services: list[str] | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"task": task}
        if services:
            arguments["services"] = services
        return await tools.handle(FIND_TOOLS, arguments)

    async def call_tool(tool_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await tools.handle(CALL_TOOL, {"tool_id": tool_id, "arguments": arguments})

    server.add_tool(find_tools, name=FIND_TOOLS, description=descriptions[FIND_TOOLS])
    server.add_tool(call_tool, name=CALL_TOOL, description=descriptions[CALL_TOOL])
    return server


class LoopbackLogin:
    """Local login UI for desktop hosts: open the browser, receive the redirect on 127.0.0.1."""

    def __init__(self, user_id: str, *, port: int = 8765, timeout: float = 300.0,
                 opener: Callable[[str], Any] = webbrowser.open) -> None:
        self.user_id = user_id
        self.port = port
        self._opener = opener
        self.broker = LoginBroker(self._show, timeout=timeout)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    async def _show(self, request: AuthorizationRequest) -> None:
        self.start()
        # The URL goes to the user's browser only; stderr keeps stdio protocol clean.
        print(f"MCPilot: opening browser to connect {request.integration_id}", file=sys.stderr)
        await asyncio.to_thread(self._opener, request.url)

    def start(self) -> None:
        if self._server is not None:
            return
        login = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                accepted = self.path.startswith("/callback?") and login.broker.complete_url(
                    user_id=login.user_id, callback_url=self.path)
                body = ("Konto połączone. Możesz zamknąć tę kartę." if accepted
                        else "Nieznane lub wygasłe logowanie.").encode()
                self.send_response(200 if accepted else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="mcpilot-login-callback")
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


@dataclass(frozen=True)
class GatewayConfig:
    """Host-owned configuration. The manifest is the administrator's approval list."""

    user_id: str
    manifest: Path
    capabilities: tuple[str, ...]
    effects: tuple[str, ...] = ("read", "draft")
    accounts: tuple[str, ...] = ("default",)
    state_dir: Path = Path("~/.mcpilot")
    allow_loopback: bool = False
    key_env: str = "MCPILOT_SECRET_KEY"
    keychain_item: str | None = None  # OS keychain entry (service "mcpilot") holding the Fernet key
    login_port: int = 8765
    connect_wait: float = 15.0
    secrets: dict[str, str] = field(default_factory=dict)  # integration id -> env var with a PAT
    audit_log: Path | None = None  # JSON Lines audit events (no arguments, results or secrets)
    registry_sync: bool = False  # keep the MCP Registry cache fresh for search suggestions
    registry_sync_interval: float = 3600.0

    @classmethod
    def load(cls, path: str | Path) -> GatewayConfig:
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)
        if not isinstance(raw, dict) or set(raw) - known:
            raise ValueError(f"Unknown gateway settings: {sorted(set(raw) - known)}")
        base = path.parent
        return cls(
            user_id=str(raw["user_id"]),
            manifest=(base / Path(raw["manifest"]).expanduser()).resolve(),
            capabilities=tuple(raw["capabilities"]),
            effects=tuple(raw.get("effects", cls.effects)),
            accounts=tuple(raw.get("accounts", cls.accounts)),
            state_dir=(base / Path(raw.get("state_dir", "~/.mcpilot")).expanduser()).resolve(),
            allow_loopback=bool(raw.get("allow_loopback", False)),
            key_env=str(raw.get("key_env", cls.key_env)),
            keychain_item=str(raw["keychain_item"]) if raw.get("keychain_item") else None,
            login_port=int(raw.get("login_port", cls.login_port)),
            connect_wait=float(raw.get("connect_wait", cls.connect_wait)),
            secrets=dict(raw.get("secrets", {})),
            audit_log=(base / Path(raw["audit_log"]).expanduser()).resolve() if raw.get("audit_log") else None,
            registry_sync=bool(raw.get("registry_sync", False)),
            registry_sync_interval=float(raw.get("registry_sync_interval", cls.registry_sync_interval)),
        )


def open_store(config: GatewayConfig, *, warn: bool = True) -> SecretStore:
    """Encrypted store with the key from the environment or the OS keychain; memory otherwise."""
    key, source = resolve_key(config.key_env, config.keychain_item)
    if key is None:
        if warn:
            # stderr only: stdout carries the MCP protocol.
            print(f"MCPilot: {source}; tokens stay in memory and every restart needs a new login",
                  file=sys.stderr)
        return MemorySecretStore()
    return EncryptedFileSecretStore(config.state_dir / "credentials", key)


async def _refresh_registry(catalog: Catalog, tools: DiscoveryTools, config: GatewayConfig) -> None:
    """Background registry sync (full once, then incremental) and index rebuild; failures keep the cache."""
    while True:
        with contextlib.suppress(Exception):
            await catalog.sync()
            tools.index = await asyncio.to_thread(CapabilityIndex.from_catalog, catalog)
        await asyncio.sleep(config.registry_sync_interval)


async def serve(config: GatewayConfig) -> None:
    catalog = Catalog(cache_path=config.state_dir / "registry-cache.json")
    catalog.load_manifest(config.manifest)
    policy = Policy(catalog.all(), capabilities=config.capabilities, effects=config.effects,
                    accounts=config.accounts, allow_loopback=config.allow_loopback)
    store = open_store(config)
    login = LoopbackLogin(config.user_id, port=config.login_port)
    auth = AuthManager(store, OAuthConfig(login=login.broker, redirect_uri=login.redirect_uri))
    for integration_id, env_var in config.secrets.items():
        secret = os.environ.get(env_var)
        if secret:
            endpoint = catalog.get(integration_id).endpoint or "stdio"
            await auth.set_secret(config.user_id, integration_id, "default", secret, endpoint=endpoint)
    try:
        async with MCPilot(user_id=config.user_id, catalog=catalog, policy=policy, auth=auth,
                           state_dir=config.state_dir,
                           audit=JsonlAuditSink(config.audit_log) if config.audit_log else None) as pilot:
            index = await asyncio.to_thread(CapabilityIndex.from_catalog, catalog)
            tools = DiscoveryTools(pilot, connect_wait=config.connect_wait, index=index)
            refresher = asyncio.create_task(_refresh_registry(catalog, tools, config)) if config.registry_sync else None
            try:
                await create_gateway(tools).run_stdio_async()
            finally:
                if refresher is not None:
                    refresher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await refresher
                await tools.close()
    finally:
        login.close()


_REMOTE_KEYS = {
    "transport", "public_url", "listen", "auth", "tenants", "state_dir", "key_env",
    "request_state_keys_env", "browser_identity_header", "max_users", "idle_timeout",
    "calls_per_minute", "connect_wait", "login_wait", "login_timeout", "audit_log",
}


def build_remote_gateway(raw: dict[str, Any], base: Path, environ: Mapping[str, str] = os.environ):
    """Validate a ``"transport": "http"`` configuration and construct a RemoteGateway."""
    from functools import partial

    from .remote import (
        JWTTokenVerifier,
        RemoteGateway,
        Tenant,
        default_principal,
        trusted_header_identity,
    )

    unknown = set(raw) - _REMOTE_KEYS
    if unknown:
        raise ValueError(f"Unknown remote gateway settings: {sorted(unknown)}")
    auth = raw["auth"]
    if set(auth) - {"issuer", "audience", "jwks_url", "jwks", "required_scopes", "tenant_claim", "user_claim"}:
        raise ValueError("Unknown auth settings")
    key = environ.get(raw.get("key_env", "MCPILOT_SECRET_KEY"))
    if not key:
        raise ValueError("A remote gateway requires an encryption key for downstream credentials")
    state_dir = (base / Path(raw.get("state_dir", "~/.mcpilot/remote")).expanduser()).resolve()
    tenants = {}
    for name, entry in raw["tenants"].items():
        if set(entry) - {"manifest", "capabilities", "effects", "accounts"}:
            raise ValueError(f"Unknown settings for tenant {name}")
        catalog = Catalog()
        catalog.load_manifest((base / Path(entry["manifest"]).expanduser()).resolve())
        tenants[name] = Tenant(catalog, Policy(
            catalog.all(), capabilities=tuple(entry["capabilities"]),
            effects=tuple(entry.get("effects", ("read", "draft"))),
            accounts=tuple(entry.get("accounts", ("default",)))))
    state_keys = environ.get(raw.get("request_state_keys_env", "MCPILOT_REQUEST_STATE_KEY"))
    header = raw.get("browser_identity_header")
    return RemoteGateway(
        tenants=tenants,
        token_verifier=JWTTokenVerifier(issuer=auth["issuer"], audience=auth["audience"],
                                        jwks_url=auth.get("jwks_url"), jwks=auth.get("jwks")),
        public_url=raw["public_url"], issuer_url=auth["issuer"], state_dir=state_dir,
        secret_store=EncryptedFileSecretStore(state_dir / "credentials", key.encode()),
        browser_identity=trusted_header_identity(header) if header else None,
        principal=partial(default_principal, tenant_claim=auth.get("tenant_claim", "tenant"),
                          user_claim=auth.get("user_claim")),
        required_scopes=tuple(auth.get("required_scopes", ())),
        request_state_keys=[state_keys.encode()] if state_keys else None,
        audit=JsonlAuditSink((base / Path(raw["audit_log"]).expanduser()).resolve()) if raw.get("audit_log") else None,
        **{k: raw[k] for k in ("max_users", "idle_timeout", "calls_per_minute", "connect_wait",
                               "login_wait", "login_timeout") if k in raw},
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mcpilot.gateway")
    parser.add_argument("--config", required=True, help="Path to the gateway JSON configuration")
    args = parser.parse_args(argv)
    path = Path(args.config)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("transport") == "http":
        gateway = build_remote_gateway(raw, path.parent)
        if not os.environ.get(raw.get("request_state_keys_env", "MCPILOT_REQUEST_STATE_KEY")):
            print("MCPilot: ephemeral request-state key; run one instance or configure a shared key",
                  file=sys.stderr)
        listen = raw.get("listen", {})
        asyncio.run(gateway.serve(listen.get("host", "127.0.0.1"), int(listen.get("port", 8000))))
        return
    asyncio.run(serve(GatewayConfig.load(path)))


if __name__ == "__main__":
    main()
