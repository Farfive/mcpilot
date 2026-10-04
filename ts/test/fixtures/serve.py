"""Real Python MCP/OAuth servers for the TypeScript SDK tests (loopback only).

Usage: python ts/test/fixtures/serve.py oauth|gateway
Prints one JSON line with the URLs, then serves until stdin closes.
"""

import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from examples.oauth_fixture import OAuthFixture  # noqa: E402


def oauth_tenant(endpoint: str):
    from mcpilot import Policy
    from mcpilot.catalog import Catalog
    from mcpilot.models import AuthSpec, Integration, ToolRule
    from mcpilot.remote import Tenant

    integration = Integration(
        id="fixture/docs", service="docs", publisher="fixture", version="1", transport="http", endpoint=endpoint,
        support="supported", capabilities=("documents.search",),
        tools={"documents_search": ToolRule(capability="documents.search")},
        auth=AuthSpec(mode="oauth", scopes_by_capability={"documents.search": ("documents.read",)}),
    )
    return Tenant(Catalog([integration]), Policy([integration], capabilities=["documents.search"], allow_loopback=True))


async def gateway(fixture: OAuthFixture) -> None:
    import uvicorn

    from mcpilot.remote import RemoteGateway, StaticTokenVerifier, trusted_header_identity

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    with tempfile.TemporaryDirectory() as state:
        remote = RemoteGateway(
            tenants={"acme": oauth_tenant(fixture.endpoint)},
            token_verifier=StaticTokenVerifier({"token-alice": {"sub": "alice", "tenant": "acme"}}),
            public_url=url, issuer_url="https://idp.example.com", state_dir=state,
            browser_identity=trusted_header_identity("X-Test-User"))
        server = uvicorn.Server(uvicorn.Config(remote.app(), log_level="warning"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(0.01)
        print(json.dumps({"gateway": url, "endpoint": fixture.endpoint}), flush=True)
        await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
        server.should_exit = True
        await task
        await remote.close()


def main() -> None:
    with OAuthFixture().serve() as fixture:
        if sys.argv[1] == "gateway":
            asyncio.run(gateway(fixture))
        else:
            print(json.dumps({"endpoint": fixture.endpoint}), flush=True)
            sys.stdin.read()


if __name__ == "__main__":
    main()
