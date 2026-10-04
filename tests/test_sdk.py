from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mcpilot import MCPilot, Policy
from mcpilot.auth import AuthManager
from mcpilot.catalog import Catalog
from mcpilot.errors import (
    BudgetExceeded,
    InvalidArguments,
    InvalidProposal,
    PolicyDenied,
    UncertainOutcome,
)
from mcpilot.integrations import github, notion, provider_candidates
from mcpilot.models import Integration, Limits, TaskRequest, ToolRule
from mcpilot.router import requirements, route

FIXTURE = Path(__file__).with_name("runtime_server.py")
FLAGSHIP = "Znajdź dokumentację projektu w Notion, porównaj ją z issue w GitHub i przygotuj podsumowanie"


def calculator(**changes) -> Integration:
    values = dict(
        id="test/calculator", service="calculator", publisher="test", version="1.0.0",
        transport="stdio", command=(sys.executable, str(FIXTURE)), support="supported",
        capabilities=("math.add", "counter.write"),
        tools={"add": ToolRule(capability="math.add"),
               "slow_write": ToolRule(capability="counter.write", effect="write"),
               "write_count": ToolRule(capability="counter.write")},
    )
    values.update(changes)
    return Integration(**values)


def pilot_for(tmp_path, integration, *, capabilities=("math.add",), effects=("read", "draft"),
              user="alice", auth=None, **kwargs) -> MCPilot:
    policy = Policy([integration], capabilities=capabilities, effects=effects)
    return MCPilot(user_id=user, catalog=Catalog([integration]), policy=policy, auth=auth,
                   state_dir=tmp_path, **kwargs)


def test_import_is_side_effect_free(tmp_path):
    code = ("import sys, mcpilot; print(any(m in sys.modules for m in "
            "('mcp', 'httpx2', 'mcpilot.runtime', 'mcpilot.sdk')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
    assert list(tmp_path.iterdir()) == []


def test_flagship_task_requirements_keep_named_services():
    found = requirements(TaskRequest(task=FLAGSHIP))
    assert {(r.service, r.capability) for r in found} == {("notion", "documents.search"), ("github", "issues.read")}
    drafted = requirements(TaskRequest(task="Przygotuj wiadomość na Slack"))
    assert [r.capability for r in drafted] == ["messages.draft"]
    sent = requirements(TaskRequest(task="Wyślij podsumowanie na Slack"))
    assert [r.capability for r in sent] == ["messages.send"]


async def test_router_respects_service_account_policy_and_candidates():
    gh, nt = github(), notion()
    drive, slack = provider_candidates()[2:]
    policy = Policy([gh, nt, drive, slack], capabilities=("issues.read", "documents.search"),
                    accounts=("default", "work"))
    request = TaskRequest(task=FLAGSHIP, accounts={"github": "work"})
    plan = await route(request, (gh, nt, drive, slack), policy)
    assert {(s.integration_id, s.account) for s in plan.selections} == {
        ("com.github/remote", "work"), ("com.notion/mcp", "default")}
    # A higher quality Drive manifest never replaces the service the user named;
    # discovered (unsupported) candidates are filtered out regardless.
    better = drive.model_copy(update={"support": "supported", "quality": 1.0})
    plan = await route(TaskRequest(task="Znajdź dokument w Notion"), (better, nt),
                       Policy([better, nt], capabilities=("documents.search",)))
    assert [s.integration_id for s in plan.selections] == ["com.notion/mcp"]
    denied = await route(TaskRequest(task="Read GitHub issues", accounts={"github": "personal"}), (gh,), policy)
    assert not denied.selections and denied.missing[0].account == "personal"


async def test_existing_connection_breaks_ties_and_reranker_is_constrained():
    first = calculator(id="test/a", quality=0.5)
    second = calculator(id="test/b", quality=0.5)
    policy = Policy([first, second], capabilities=("math.add",))
    request = TaskRequest(task="add numbers", capabilities=("math.add",))
    assert (await route(request, (first, second), policy)).selections[0].integration_id == "test/a"
    reused = await route(request, (first, second), policy, connected={("test/b", "default")})
    assert reused.selections[0].integration_id == "test/b"

    async def picks(value):
        async def reranker(requirement, candidates):
            assert all(set(c) == {"id", "service", "capabilities", "score"} for c in candidates)
            return value
        return reranker

    chosen = await route(request, (first, second), policy, reranker=await picks("test/b"))
    assert chosen.selections[0].integration_id == "test/b"
    for invented in ("https://evil.example/mcp", "npx evil-package", "test/unapproved"):
        with pytest.raises(InvalidProposal):
            await route(request, (first, second), policy, reranker=await picks(invented))


def test_policy_pins_exact_manifest_and_endpoint_rules():
    approved = github()
    policy = Policy([approved], capabilities=("issues.read",))
    policy.check(approved, "issues.read")
    swapped = approved.model_copy(update={"endpoint": "https://evil.example/mcp"})
    with pytest.raises(PolicyDenied, match="not approved"):
        policy.check(swapped, "issues.read")
    with pytest.raises(PolicyDenied):
        policy.check(approved, "repo.write")
    for endpoint in ("http://api.example/mcp", "https://10.0.0.1/mcp", "https://127.0.0.1/mcp",
                     "https://user:pw@api.example/mcp", "https://api.example/mcp?token=1"):
        with pytest.raises(PolicyDenied):
            policy.check_endpoint(endpoint)
    Policy(allow_loopback=True).check_endpoint("http://127.0.0.1:9/mcp")


async def test_stdio_task_to_call_hides_unmapped_and_disallowed_tools(tmp_path):
    async with pilot_for(tmp_path, calculator(), capabilities=("math.add", "counter.write")) as pilot:
        plan = await pilot.plan(TaskRequest(task="add", capabilities=("math.add", "counter.write")))
        toolset = await pilot.tools_for(plan)
        assert [c.status for c in toolset.connections] == ["ready"]
        # process_info is unmapped; slow_write is a write and policy allows only read/draft.
        assert sorted(t.name for t in toolset.tools) == ["add", "write_count"]
        assert all(t.untrusted for t in toolset.tools)
        add = next(t for t in toolset.tools if t.name == "add")
        result = await pilot.call(add.id, {"left": 2, "right": 3})
        assert result.structured_content == {"sum": 5} and result.untrusted
        with pytest.raises(InvalidArguments):
            await pilot.call(add.id, {"left": "two", "right": 3})
        with pytest.raises(PolicyDenied, match="not exposed"):
            await pilot.call("mcp_" + "0" * 32, {})
        status = await pilot.status()
        assert [(c.status, c.capabilities) for c in status] == [("ready", ("counter.write", "math.add"))]
        await pilot.disconnect("test/calculator")
        assert (await pilot.status())[0].status == "disconnected"
        with pytest.raises(PolicyDenied):
            await pilot.call(add.id, {"left": 1, "right": 1})


async def test_token_budget_and_step_limit(tmp_path):
    integration = calculator()
    async with pilot_for(tmp_path, integration, capabilities=("math.add", "counter.write"),
                         limits=Limits(tool_tokens=600, max_steps=1)) as pilot:
        request = TaskRequest(task="add", capabilities=("math.add", "counter.write"))
        small = await pilot.tools_for(request, token_budget=40)
        assert small.truncated and not small.tools
        toolset = await pilot.tools_for(request)
        assert toolset.token_estimate <= 600
        add = next(t for t in toolset.tools if t.name == "add")
        await pilot.call(add.id, {"left": 1, "right": 1})
        with pytest.raises(BudgetExceeded):
            await pilot.call(add.id, {"left": 1, "right": 1})


async def test_uncertain_write_is_never_replayed(tmp_path):
    async with pilot_for(tmp_path, calculator(), capabilities=("counter.write",),
                         effects=("read", "write")) as pilot:
        toolset = await pilot.tools_for(TaskRequest(task="count", capabilities=("counter.write",)))
        tools = {t.name: t for t in toolset.tools}
        assert tools["slow_write"].effect == "write"
        pilot._sessions[toolset.connections[0].id].timeout = 0.2
        with pytest.raises(UncertainOutcome):
            await pilot.call(tools["slow_write"].id, {"delay": 0.6})
        pilot._sessions[toolset.connections[0].id].timeout = 5
        count = await pilot.call(tools["write_count"].id, {})
        assert count.structured_content == {"calls": 1}


async def test_users_and_accounts_are_isolated(tmp_path):
    remote = github("https://api.example/mcp").model_copy(update={"support": "supported"})
    auth = AuthManager()
    await auth.set_secret("alice", remote.id, "work", "alice-token", endpoint=remote.endpoint)
    policy = Policy([remote], capabilities=("issues.read",), accounts=("default", "work"))
    async with MCPilot(user_id="bob", catalog=Catalog([remote]), policy=policy, auth=auth,
                       state_dir=tmp_path / "bob") as bob:
        assert (await bob.connect(remote.id, account="work")).status == "auth_required"
    async with MCPilot(user_id="alice", catalog=Catalog([remote]), policy=policy, auth=auth,
                       state_dir=tmp_path / "alice") as alice:
        assert (await alice.connect(remote.id, account="default")).status == "auth_required"
        handle = await auth.prepare("alice", remote, "work", ("issues.read",))
        assert handle.status == "ready" and "alice-token" not in repr(handle)
        assert "alice-token" not in str(handle.public_state())
    with pytest.raises(ValueError):
        MCPilot(user_id="", catalog=Catalog(), policy=policy)


async def test_setup_required_is_explained_and_reviewed_candidates_report_it(tmp_path):
    drive, slack = provider_candidates()[2:]
    policy = Policy([drive, slack], capabilities=("documents.search", "messages.search"))
    async with MCPilot(user_id="alice", catalog=Catalog([drive, slack]), policy=policy, state_dir=tmp_path) as pilot:
        plan = await pilot.plan("Znajdź dokument w Google Drive")
        assert not plan.selections and plan.missing[0].service == "google-drive"
        assert [(u.integration_id, u.reason) for u in plan.unavailable] == [("com.google/drive", "setup_required")]
        assert await pilot.discover("Znajdź dokument w Google Drive") == []

    # After the host reviews a tool mapping, connecting still needs the provider app.
    reviewed = [c.model_copy(update={"support": "supported", "tools": {"search": ToolRule(capability=c.capabilities[0])}})
                for c in (drive, slack)]
    policy = Policy(reviewed, capabilities=("documents.search", "messages.search"))
    async with MCPilot(user_id="alice", catalog=Catalog(reviewed), policy=policy, state_dir=tmp_path) as pilot:
        assert await pilot.discover("Znajdź dokument w Google Drive") == [{
            "id": "com.google/drive", "service": "google-drive", "capability": "documents.search",
            "account": "default", "support": "supported", "auth": "oauth"}]
        for integration in reviewed:
            connection = await pilot.connect(integration.id)
            assert connection.status == "setup_required" and connection.capabilities


async def test_audit_events_are_secret_free_and_sink_failures_do_not_break_calls(tmp_path):
    events = []
    async with pilot_for(tmp_path, calculator(), audit=events.append) as pilot:
        toolset = await pilot.tools_for(TaskRequest(task="add", capabilities=("math.add",)))
        second = await pilot.tools_for(TaskRequest(task="add", capabilities=("math.add",)))  # reuse: no event
        add = toolset.tools[0]
        await pilot.call(add.id, {"left": 1, "right": 2})
        with pytest.raises(InvalidArguments):
            await pilot.call(add.id, {"left": "secret-argument"})
        await pilot.disconnect("test/calculator")
    assert [(e.action, e.outcome, e.tool) for e in events] == [
        ("connect", "ready", None), ("call", "ok", "add"), ("call", "InvalidArguments", "add"),
        ("disconnect", "ok", None)]
    assert all(e.user_id == "alice" for e in events)
    # The connect belongs to the first task; calls are charged to the latest task that issued the tool.
    assert events[0].task_id == toolset.task_id != second.task_id
    assert events[1].task_id == events[2].task_id == second.task_id
    assert all(e.duration_ms is not None and e.duration_ms >= 0 for e in events[:3])
    assert "secret-argument" not in str([e.model_dump() for e in events])

    def broken(_):
        raise RuntimeError("sink down")

    async with pilot_for(tmp_path, calculator(), audit=broken) as pilot:
        toolset = await pilot.tools_for(TaskRequest(task="add", capabilities=("math.add",)))
        assert (await pilot.call(toolset.tools[0].id, {"left": 2, "right": 2})).structured_content == {"sum": 4}


async def test_budget_is_per_task_not_per_session_so_gateway_sessions_keep_working(tmp_path):
    from mcpilot.discovery import CALL_TOOL, FIND_TOOLS, DiscoveryTools

    events = []
    async with pilot_for(tmp_path, calculator(), audit=events.append, tenant="acme") as pilot:
        tools = DiscoveryTools(pilot)

        async def add_tool_id():
            found = await tools.handle(FIND_TOOLS, {"task": "policz", "services": ["calculator"]})
            return next(t["id"] for t in found["tools"] if t["name"] == "add")

        tool_id = await add_tool_id()
        results = [await tools.handle(CALL_TOOL, {"tool_id": tool_id, "arguments": {"left": i, "right": 1}})
                   for i in range(21)]
        assert sum("error" not in r for r in results) == 20  # Limits.max_steps per task
        assert results[20]["error"] == "BudgetExceeded" and "new task" in results[20]["message"]
        tool_id = await add_tool_id()  # the model requests tools again: a new task
        for i in range(5):
            assert (await tools.handle(CALL_TOOL, {"tool_id": tool_id, "arguments": {"left": i, "right": 1}})
                    )["structured_content"] == {"sum": i + 1}
    calls = [e for e in events if e.action == "call"]
    assert len({e.task_id for e in calls}) == 2 and all(e.tenant == "acme" for e in events)
