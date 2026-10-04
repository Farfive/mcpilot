"""Remote multi-user gateway with URL-elicitation login, end to end on loopback.

Run from the repository: python -m examples.remote_gateway_demo

DEMONSTRATIVE. Real: Streamable HTTP, MCP authorization of the host (bearer
token -> principal), per-user sessions, URL elicitation (protocol 2026-07-28),
downstream OAuth with PKCE. Simulated: the organization IdP (static tokens),
the provider (fixture) and the user's browser clicks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import tempfile

import httpx2
import mcp_types as types
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from examples.oauth_fixture import OAuthFixture
from mcpilot import Policy
from mcpilot.catalog import Catalog
from mcpilot.models import AuthSpec, Integration, ToolRule
from mcpilot.remote import RemoteGateway, StaticTokenVerifier, Tenant, trusted_header_identity

TOKENS = {"token-alice": {"sub": "alice", "tenant": "acme"}, "token-bob": {"sub": "bob", "tenant": "acme"}}


@contextlib.asynccontextmanager
async def serve_gateway(fixture: OAuthFixture, state_dir: str):
    integration = Integration(
        id="fixture/docs", service="docs", publisher="fixture", version="1", transport="http",
        endpoint=fixture.endpoint, support="supported", capabilities=("documents.search",),
        tools={"documents_search": ToolRule(capability="documents.search")},
        auth=AuthSpec(mode="oauth", scopes_by_capability={"documents.search": ("documents.read",)}),
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    gateway = RemoteGateway(
        tenants={"acme": Tenant(Catalog([integration]), Policy([integration], capabilities=["documents.search"],
                                                                allow_loopback=True))},
        token_verifier=StaticTokenVerifier(TOKENS), public_url=url, issuer_url="https://idp.example.com",
        state_dir=state_dir, browser_identity=trusted_header_identity("X-Demo-User"))
    server = uvicorn.Server(uvicorn.Config(gateway.app(), log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield gateway, url
    finally:
        server.should_exit = True
        await task
        await gateway.close()


@contextlib.asynccontextmanager
async def host_client(url: str, token: str, **kwargs):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http), **kwargs) as client:
            yield client


async def run_demo(state_dir: str) -> dict:
    report: dict = {"label": "demonstracyjne: prawdziwe MCP/HTTP/OAuth, symulowani IdP, dostawca i przeglądarka"}
    with OAuthFixture().serve() as fixture:
        async with serve_gateway(fixture, state_dir) as (gateway, url):
            browsers: list[asyncio.Task] = []

            async def user_browser(link: str, user: str) -> int:
                async with httpx2.AsyncClient(follow_redirects=False, headers={"X-Demo-User": user}) as tab:
                    page = await tab.get(link)
                    if page.status_code != 302:
                        return page.status_code
                    callback = await fixture.approve(page.headers["location"])
                    return (await tab.get(callback)).status_code

            async def show_link(context, params):
                # The host renders a "Connect" prompt; the URL never reaches the model.
                report["elicitation"] = {"mode": params.mode, "gateway_page": params.url.split("/connect/")[0] + "/connect/…",
                                         "message": params.message}
                report["forwarded_to_bob"] = await user_browser(params.url, "bob")
                browsers.append(asyncio.create_task(user_browser(params.url, "alice")))
                return types.ElicitResult(action="accept")

            async with host_client(url, "token-alice", elicitation_callback=show_link) as alice:
                found = (await alice.call_tool("mcpilot_find_tools", {"task": "Szukaj dokumentów", "services": ["docs"]})
                         ).structured_content
                report["alice_browser"] = (await asyncio.gather(*browsers))[0]
                report["alice_tools"] = [t["name"] for t in found["tools"]]
                result = await alice.call_tool("mcpilot_call_tool", {"tool_id": found["tools"][0]["id"],
                                                                     "arguments": {"query": "atlas"}})
                report["alice_result"] = result.structured_content["content"][0]["text"]
                report["model_saw_login_url"] = "/connect/" in json.dumps(found)
            async with host_client(url, "token-bob") as bob:
                bob_view = (await bob.call_tool("mcpilot_find_tools", {"task": "Szukaj dokumentów", "services": ["docs"]})
                            ).structured_content
                report["bob_needs_own_login"] = bob_view["needs_user"][0]["action"]
            report["active_users"] = gateway.active_users()
            report["provider_logins"] = fixture.authorization_count
    return report


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mcpilot-remote-") as directory:
        print(json.dumps(await run_demo(directory), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
