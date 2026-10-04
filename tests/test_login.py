from __future__ import annotations

import asyncio
import threading

import pytest
from oauth_fixture import OAuthFixture

from examples.notion_github_task import run_scenario
from mcpilot import MCPilot, Policy
from mcpilot.auth import AuthManager, AuthorizationRequest, AuthRequired, LoginBroker, OAuthConfig
from mcpilot.catalog import Catalog
from mcpilot.models import AuthSpec, Integration, ToolRule


def request(user="alice", connection="conn_a", state="s1"):
    return AuthorizationRequest(connection, "fixture/docs", "default", (),
                                f"https://idp.example/authorize?state={state}&code_challenge=x", user)


async def test_complete_requires_matching_user_and_state():
    broker = LoginBroker(timeout=5)
    pending = request()
    await broker.begin(pending)
    waiter = asyncio.create_task(broker.wait(pending))
    await asyncio.sleep(0)
    assert broker.pending("alice") == (pending,)
    assert broker.pending("bob") == ()
    assert not broker.complete(user_id="bob", state="s1", code="stolen")
    assert not broker.complete(user_id="alice", state="other", code="code")
    assert broker.complete_url(user_id="alice", callback_url="https://host/cb?code=abc&state=s1&iss=https://idp")
    result = await waiter
    assert (result.code, result.state, result.iss) == ("abc", "s1", "https://idp")
    assert broker.pending("alice") == ()
    assert not broker.complete(user_id="alice", state="s1", code="replay")


async def test_denied_consent_timeout_cancel_and_supersede_are_auth_required():
    broker = LoginBroker(timeout=0.05)
    denied = request(state="denied")
    await broker.begin(denied)
    waiter = asyncio.create_task(broker.wait(denied))
    await asyncio.sleep(0)
    assert broker.complete_url(user_id="alice", callback_url="https://host/cb?error=access_denied&state=denied")
    with pytest.raises(AuthRequired):
        await waiter

    slow = request(state="slow")
    await broker.begin(slow)
    with pytest.raises(AuthRequired, match="in time"):
        await broker.wait(slow)
    assert broker.pending("alice") == ()

    broker.timeout = 5
    old, new = request(state="old"), request(state="new")
    await broker.begin(old)
    old_waiter = asyncio.create_task(broker.wait(old))
    await asyncio.sleep(0)
    await broker.begin(new)
    with pytest.raises(AuthRequired, match="superseded"):
        await old_waiter
    new_waiter = asyncio.create_task(broker.wait(new))
    await asyncio.sleep(0)
    assert broker.cancel("alice", "conn_a")
    with pytest.raises(AuthRequired, match="cancelled"):
        await new_waiter


async def test_complete_from_another_thread_resolves_on_waiting_loop():
    broker = LoginBroker(timeout=5)
    pending = request()
    await broker.begin(pending)
    waiter = asyncio.create_task(broker.wait(pending))
    await asyncio.sleep(0)
    accepted = []
    thread = threading.Thread(target=lambda: accepted.append(
        broker.complete(user_id="alice", state="s1", code="threaded")))
    thread.start()
    thread.join()
    assert accepted == [True]
    assert (await waiter).code == "threaded"


async def test_unanswered_login_returns_auth_required_connection_not_error(tmp_path):
    with OAuthFixture().serve() as fixture:
        integration = Integration(
            id="fixture/docs", service="docs", publisher="fixture", version="1", transport="http",
            endpoint=fixture.endpoint, support="supported", capabilities=("documents.search",),
            tools={"documents_search": ToolRule(capability="documents.search")},
            auth=AuthSpec(mode="oauth", scopes_by_capability={"documents.search": ("documents.read",)}),
        )
        buttons = []

        async def show(event):
            buttons.append(event)

        manager = AuthManager()
        manager.configure_oauth(integration.id, OAuthConfig(login=LoginBroker(show, timeout=0.2)))
        policy = Policy([integration], capabilities=["documents.search"], allow_loopback=True)
        async with MCPilot(user_id="alice", catalog=Catalog([integration]), policy=policy,
                           auth=manager, state_dir=tmp_path) as pilot:
            connection = await pilot.connect(integration.id)
        assert connection.status == "auth_required"
        assert buttons and buttons[0].user_id == "alice"
        assert "authorize" not in (connection.message or "")


async def test_flagship_notion_github_scenario(tmp_path):
    report = await run_scenario(tmp_path)
    assert report["plan"] == [("com.github/remote", "issues.read"), ("com.notion/mcp", "documents.search")]
    assert all("endpoint" not in item and "command" not in item for item in report["discovery"])
    assert report["first_resume"] == "waiting_for_auth"
    assert report["steps_after_first_resume"] == ["complete", "complete", "pending", "pending"]
    assert report["connections_before_login"] == {"com.github/remote": "ready", "com.notion/mcp": "auth_required"}
    assert report["login_button"]["integration"] == "com.notion/mcp"
    assert report["final_status"] == "complete"
    assert report["github_calls"] == 2  # completed steps were not replayed after login
    assert report["notion_logins"] == 1
    assert report["after_restart"] == [("com.github/remote", "ready"), ("com.notion/mcp", "ready")]
    assert report["reread_ok"]
    assert report["summary"]["status"] == "draft" and report["summary"]["sent"] is False
    for path in (tmp_path / "credentials").iterdir():
        content = path.read_bytes()
        assert b"fixture-access-token" not in content and b"ghp_fixture" not in content


async def test_failed_notification_leaves_no_pending_login():
    async def broken(_):
        raise RuntimeError("websocket closed")

    broker = LoginBroker(broken)
    with pytest.raises(RuntimeError):
        await broker.begin(request())
    assert broker.pending("alice") == ()
    assert not broker.complete(user_id="alice", state="s1", code="late")
