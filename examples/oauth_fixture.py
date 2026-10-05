"""Deterministic OAuth authorization-server and MCP fixture, not a vendor integration.

The real official SDK drives discovery, DCR, PKCE, state/issuer verification,
code exchange and refresh. Only the HTTP authorization server and the provider
data are replaced. ``static_tokens`` simulates a personal access token (PAT).
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2
from mcp.client.auth import AuthorizationCodeResult


@dataclass(frozen=True)
class FixtureTool:
    description: str
    input_schema: dict
    handler: Callable[[dict], str]
    annotations: dict | None = None  # MCP tool annotations, e.g. {"readOnlyHint": True}


DEFAULT_TOOLS = {
    "documents_search": FixtureTool(
        "Search fixture documents",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        lambda _: "OAuth protected fixture document",
    ),
}


class OAuthFixture:
    endpoint = "https://resource.fixture.test/mcp"
    issuer = "https://identity.fixture.test/issuer"

    def __init__(
        self,
        *,
        tools: dict[str, FixtureTool] | None = None,
        expected_scope: str | None = "documents.read",
        static_tokens: tuple[str, ...] = (),
    ) -> None:
        self.tools = DEFAULT_TOOLS if tools is None else tools
        self.expected_scope = expected_scope
        self.static_tokens = static_tokens
        self.calls: list[tuple[str, dict]] = []
        self.authorization_count = 0
        self.registration_count = 0
        self.refresh_count = 0
        self.token_count = 0
        self.requests: list[tuple[str, str]] = []
        self.authorization: dict[str, list[str]] = {}
        self.granted_scope = expected_scope
        self.fail_state = False
        self.fail_issuer = False
        self.token_error: str | None = None
        self.challenge_scope: str | None = None
        self.registration_scope: str | None = None
        self._serving = False
        self._callback_parameters: dict[str, list[str]] | None = None

    async def approve(self, url: str) -> str:
        """Simulate the user's consent click; return the provider redirect to the host."""
        async with httpx2.AsyncClient(follow_redirects=False) as client:
            response = await client.get(url + "&fixture_consent=allow")
        assert response.status_code == 302
        return response.headers["location"]

    async def redirect(self, url: str) -> None:
        if self._serving:
            # Only this clearly labelled fixture auto-approves consent. A real
            # host renders the URL and waits for its authenticated callback.
            location = await self.approve(url)
            self._callback_parameters = parse_qs(urlparse(location).query)
            return
        self._record_authorization(url)

    def _record_authorization(self, url: str) -> None:
        parsed = httpx2.URL(url)
        assert str(parsed).startswith(self.issuer + "/authorize?")
        self.authorization = parse_qs(parsed.query.decode())
        assert self.authorization["code_challenge_method"] == ["S256"]
        # Exactly the policy scope; offline_access (refresh tokens) is the only tolerated addition.
        requested = set((self.authorization.get("scope") or [""])[0].split()) - {"offline_access"}
        assert requested == ({self.expected_scope} if self.expected_scope else set())
        self.authorization_count += 1

    async def callback(self) -> AuthorizationCodeResult:
        return AuthorizationCodeResult(
            code=self._callback_parameters["code"][0] if self._callback_parameters else "fixture-authorization-code",
            state="wrong-state" if self.fail_state else self.authorization["state"][0],
            iss="https://wrong-issuer.test" if self.fail_issuer else self.issuer,
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append((request.method, str(request.url)))
        url = str(request.url)
        if url.startswith(self.issuer + "/authorize?"):
            params = parse_qs(request.url.query.decode())
            if params.get("fixture_consent") != ["allow"]:
                fields = "".join(f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(values[0])}">'
                                 for key, values in params.items())
                return httpx2.Response(200, text=(
                    '<h1>MCPilot fixture consent</h1><p>Local demonstration; no vendor account is used.</p>'
                    f'<form method="get">{fields}<button name="fixture_consent" value="allow">Allow fixture access</button></form>'
                ), headers={"Content-Type": "text/html"})
            self._record_authorization(url)
            callback = params["redirect_uri"][0] + "?" + urlencode({
                "code": "fixture-authorization-code", "state": params["state"][0], "iss": self.issuer,
            })
            return httpx2.Response(302, headers={"Location": callback})
        if url == self.endpoint:
            bearer = request.headers.get("authorization")
            accepted = {"Bearer fixture-access-token", "Bearer fixture-refreshed-token",
                        *(f"Bearer {token}" for token in self.static_tokens)}
            if bearer in accepted:
                if request.method == "POST" and request.content:
                    return self._rpc(json.loads(request.content))
                if "text/event-stream" in request.headers.get("accept", ""):
                    return httpx2.Response(405)
                return httpx2.Response(200, json={"result": "authorized fixture response"})
            resource_metadata = self.endpoint.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp"
            challenge = f'Bearer resource_metadata="{resource_metadata}"'
            if self.challenge_scope:
                challenge += f', scope="{self.challenge_scope}"'
            return httpx2.Response(401, headers={"WWW-Authenticate": challenge})
        if url == self.endpoint.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(200, json={
                "resource": self.endpoint,
                "authorization_servers": [self.issuer],
                "scopes_supported": ["documents.read", "documents.write", "offline_access"],
            })
        if ".well-known/oauth-authorization-server" in url or ".well-known/openid-configuration" in url:
            return httpx2.Response(200, json={
                "issuer": self.issuer,
                "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token",
                "registration_endpoint": self.issuer + "/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "authorization_response_iss_parameter_supported": True,
                "scopes_supported": ["documents.read", "documents.write", "offline_access"],
            })
        if url == self.issuer + "/register":
            self.registration_count += 1
            metadata = json.loads(request.content)
            self.registration_scope = metadata.get("scope")
            return httpx2.Response(201, json={**metadata, "client_id": "fixture-client"})
        if url == self.issuer + "/token":
            form = parse_qs(request.content.decode())
            if self.token_error:
                return httpx2.Response(400, text=self.token_error)
            if form["grant_type"] == ["refresh_token"]:
                assert form["refresh_token"] == ["fixture-refresh-token"]
                self.refresh_count += 1
                return httpx2.Response(200, json={
                    "access_token": "fixture-refreshed-token", "token_type": "Bearer", "expires_in": 3600,
                })
            assert form["grant_type"] == ["authorization_code"]
            assert form["code"] == ["fixture-authorization-code"]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            assert self.authorization["code_challenge"] == [challenge]
            assert form["redirect_uri"] == self.authorization["redirect_uri"]
            assert form["resource"] == [self.endpoint]
            self.token_count += 1
            token = {
                "access_token": "fixture-access-token", "refresh_token": "fixture-refresh-token",
                "token_type": "Bearer", "expires_in": 3600,
            }
            if self.granted_scope:
                token["scope"] = self.granted_scope
            return httpx2.Response(200, json=token)
        return httpx2.Response(404)

    def _rpc(self, message: dict) -> httpx2.Response:
        method = message.get("method")
        if method == "notifications/initialized":
            return httpx2.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oauth-fixture", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": tool.description, "inputSchema": tool.input_schema,
                                 **({"annotations": tool.annotations} if tool.annotations else {})}
                                for name, tool in self.tools.items()]}
        elif method == "tools/call":
            name = message["params"]["name"]
            arguments = message["params"].get("arguments") or {}
            if name not in self.tools:
                return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"],
                                                  "error": {"code": -32602, "message": "Unknown tool"}})
            self.calls.append((name, arguments))
            result = {"content": [{"type": "text", "text": self.tools[name].handler(arguments)}]}
        elif method == "ping":
            result = {}
        else:
            return httpx2.Response(400)
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    @contextmanager
    def serve(self):
        """Expose the fixture over real loopback HTTP for transport-level tests."""
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def do_DELETE(self):
                self.respond()

            def respond(self):
                size = int(self.headers.get("Content-Length", "0"))
                request = httpx2.Request(self.command, f"http://{self.headers['Host']}{self.path}",
                    headers=dict(self.headers), content=self.rfile.read(size) if size else b"")
                response = fixture(request)
                self.send_response(response.status_code)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                if "content-length" not in response.headers:
                    self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
        self.issuer = f"http://127.0.0.1:{server.server_port}/issuer"
        self._serving = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            self._serving = False
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
