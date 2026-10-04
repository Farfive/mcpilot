"""Real loopback HTTP OAuth + MCP; simulated identity provider, no real account.

Run from the repository: python -m examples.oauth_demo
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from examples.oauth_fixture import OAuthFixture
from mcpilot.auth import AuthManager, EncryptedFileSecretStore, OAuthConfig
from mcpilot.catalog import Catalog
from mcpilot.models import AuthSpec, Integration, ToolRule
from mcpilot.policy import Policy
from mcpilot.sdk import MCPilot


async def run_demo(state_dir: Path) -> dict:
    # A deployed host supplies a persistent key from its secret manager. This
    # ephemeral demo deliberately discards its key and directory on exit.
    encryption_key = Fernet.generate_key()
    store = EncryptedFileSecretStore(state_dir / "credentials", encryption_key)
    with OAuthFixture().serve() as fixture:
        integration = Integration(
            id="fixture/oauth-documents", service="fixture-documents", publisher="MCPilot demo",
            version="1.0.0", transport="http", endpoint=fixture.endpoint, support="supported",
            capabilities=("documents.search",),
            tools={"documents_search": ToolRule(capability="documents.search")},
            auth=AuthSpec(mode="oauth", scopes_by_capability={"documents.search": ("documents.read",)}),
        )
        policy = Policy([integration], capabilities=("documents.search",), allow_loopback=True)
        catalog = Catalog([integration])
        manager = AuthManager(store)
        task = "Find project documentation"
        async with MCPilot(user_id="fixture-user", catalog=catalog, policy=policy, auth=manager,
                           state_dir=state_dir) as pilot:
            plan = await pilot.plan(task)
            waiting = await pilot.tools_for(plan)
            assert waiting.connections[0].status == "auth_required"
            assert not waiting.tools
            ui_events = []

            async def ui(event):
                # In a real application render event.url as a login button in
                # the authenticated host UI; never include it in model messages.
                ui_events.append(event.connection_id)
                await fixture.redirect(event.url)

            manager.configure_oauth(integration.id, OAuthConfig(
                authorization_handler=ui, callback_handler=fixture.callback,
            ))
            ready = await pilot.tools_for(plan)
            assert ready.connections[0].status == "ready"
            result = await pilot.call(ready.tools[0].id, {"query": "project"})
            await pilot.disconnect(integration.id)

        # A different manager reloads encrypted tokens and DCR registration;
        # no login callback is available for this second connection.
        restarted = AuthManager(EncryptedFileSecretStore(state_dir / "credentials", encryption_key), OAuthConfig())
        async with MCPilot(user_id="fixture-user", catalog=catalog, policy=policy, auth=restarted,
                           state_dir=state_dir) as pilot:
            reused = await pilot.tools_for(task)
            assert reused.connections[0].status == "ready"
            again = await pilot.call(reused.tools[0].id, {"query": "project"})
        return {
            "fixture": "Simulated identity provider; real loopback HTTP, OAuth PKCE and MCP",
            "initial_status": waiting.connections[0].status,
            "after_login": ready.connections[0].status,
            "after_restart": reused.connections[0].status,
            "result": result.content,
            "repeat_result": again.content,
            "browser_logins": fixture.authorization_count,
            "client_registrations": fixture.registration_count,
            "ui_events": len(ui_events),
        }


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mcpilot-oauth-") as directory:
        print(json.dumps(await run_demo(Path(directory)), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
