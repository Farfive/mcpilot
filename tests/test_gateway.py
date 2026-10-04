from __future__ import annotations

import asyncio
import json

import httpx2
from mcp import Client
from oauth_fixture import OAuthFixture
from test_sdk import calculator, pilot_for

from mcpilot import MCPilot, Policy
from mcpilot.auth import AuthManager, AuthorizationRequest, LoginBroker, OAuthConfig
from mcpilot.catalog import Catalog
from mcpilot.discovery import CALL_TOOL, FIND_TOOLS, DiscoveryTools
from mcpilot.gateway import GatewayConfig, LoopbackLogin, create_gateway
from mcpilot.models import AuthSpec, Integration, ToolRule


async def test_gateway_exposes_only_meta_tools_and_runs_task_to_call(tmp_path):
    async with pilot_for(tmp_path, calculator(), capabilities=("math.add",)) as pilot:
        tools = DiscoveryTools(pilot)
        async with Client(create_gateway(tools)) as client:
            listed = await client.list_tools()
            assert sorted(t.name for t in listed.tools) == [CALL_TOOL, FIND_TOOLS]
            found = (await client.call_tool(FIND_TOOLS, {"task": "add numbers", "services": ["calculator"]}))
            payload = found.structured_content
            assert [t["name"] for t in payload["tools"]] == ["add"]
            assert payload["connections"][0]["status"] == "ready" and payload["needs_user"] == []
            serialized = json.dumps(payload)
            assert "runtime_server.py" not in serialized and "command" not in serialized
            tool_id = payload["tools"][0]["id"]
            called = await client.call_tool(CALL_TOOL, {"tool_id": tool_id, "arguments": {"left": 4, "right": 5}})
            assert called.structured_content["structured_content"] == {"sum": 9}
            assert called.structured_content["untrusted"] is True
            rejected = await client.call_tool(CALL_TOOL, {"tool_id": tool_id, "arguments": {"left": "x"}})
            assert rejected.structured_content["error"] == "InvalidArguments"
        await tools.close()


async def test_discovery_rejects_unknown_tools_and_malformed_arguments(tmp_path):
    async with pilot_for(tmp_path, calculator(), capabilities=("math.add",)) as pilot:
        tools = DiscoveryTools(pilot)
        assert [d["name"] for d in tools.definitions()] == [FIND_TOOLS, CALL_TOOL]
        assert (await tools.handle("shell", {}))["error"] == "UnknownTool"
        assert (await tools.handle(FIND_TOOLS, {"task": "x", "endpoint": "https://evil"}))["error"] == "InvalidArguments"
        bad_id = await tools.handle(CALL_TOOL, {"tool_id": "../../etc", "arguments": {}})
        assert bad_id["error"] == "InvalidArguments"
        unknown = await tools.handle(CALL_TOOL, {"tool_id": "mcp_" + "a" * 32, "arguments": {}})
        assert unknown["error"] == "PolicyDenied"


async def test_login_does_not_block_the_agent_loop(tmp_path):
    with OAuthFixture().serve() as fixture:
        integration = Integration(
            id="fixture/docs", service="docs", publisher="fixture", version="1", transport="http",
            endpoint=fixture.endpoint, support="supported", capabilities=("documents.search",),
            tools={"documents_search": ToolRule(capability="documents.search")},
            auth=AuthSpec(mode="oauth", scopes_by_capability={"documents.search": ("documents.read",)}),
        )
        buttons: asyncio.Queue[AuthorizationRequest] = asyncio.Queue()
        broker = LoginBroker(buttons.put, timeout=30)
        auth = AuthManager(oauth_config=OAuthConfig(login=broker))
        policy = Policy([integration], capabilities=["documents.search"], allow_loopback=True)
        async with MCPilot(user_id="alice", catalog=Catalog([integration]), policy=policy,
                           auth=auth, state_dir=tmp_path) as pilot:
            tools = DiscoveryTools(pilot, connect_wait=0.3)
            first = await tools.handle(FIND_TOOLS, {"task": "search docs", "services": ["docs"]})
            assert first["tools"] == []
            assert first["needs_user"] == [{"integration_id": "fixture/docs", "account": "default",
                                            "action": "finish_login"}]
            button = await asyncio.wait_for(buttons.get(), 5)
            assert button.url not in json.dumps(first)
            assert broker.complete_url(user_id="alice", callback_url=await fixture.approve(button.url))
            for _ in range(50):
                second = await tools.handle(FIND_TOOLS, {"task": "search docs", "services": ["docs"]})
                if second["tools"]:
                    break
                await asyncio.sleep(0.05)
            assert [t["name"] for t in second["tools"]] == ["documents_search"]
            result = await tools.handle(CALL_TOOL, {"tool_id": second["tools"][0]["id"], "arguments": {"query": "x"}})
            assert result["content"][0]["text"] == "OAuth protected fixture document"
            await tools.close()


async def test_loopback_login_callback_completes_only_known_state():
    opened = []
    login = LoopbackLogin("alice", port=0, opener=opened.append)
    login.start()
    try:
        request = AuthorizationRequest("conn_1", "fixture/docs", "default", (),
                                       "https://idp.example/authorize?state=abc", "alice")
        await login.broker.begin(request)
        assert opened == [request.url]
        waiter = asyncio.create_task(login.broker.wait(request))
        async with httpx2.AsyncClient() as client:
            wrong = await client.get(f"http://127.0.0.1:{login.port}/callback?code=x&state=other")
            right = await client.get(f"http://127.0.0.1:{login.port}/callback?code=x&state=abc")
        assert (wrong.status_code, right.status_code) == (400, 200)
        assert (await asyncio.wait_for(waiter, 5)).code == "x"
    finally:
        login.close()


def test_gateway_config_resolves_paths_and_rejects_unknown_keys(tmp_path):
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"user_id": "me", "manifest": "approved.json",
                                "capabilities": ["files.read"], "state_dir": "state"}))
    config = GatewayConfig.load(path)
    assert config.manifest == tmp_path / "approved.json" and config.state_dir == tmp_path / "state"
    assert config.effects == ("read", "draft")
    path.write_text(json.dumps({"user_id": "me", "manifest": "a.json", "capabilities": [], "command": "rm"}))
    try:
        GatewayConfig.load(path)
    except ValueError as exc:
        assert "command" in str(exc)
    else:
        raise AssertionError("unknown key accepted")


async def test_gateway_cli_over_real_stdio_with_nested_local_server(tmp_path):
    import sys
    from pathlib import Path

    from mcp import StdioServerParameters

    from examples.gateway_setup import write_config

    workspace = Path(__file__).parent.parent / "examples" / "workspace"
    config = write_config(tmp_path / "gateway", workspace)
    settings = json.loads(config.read_text())
    config.write_text(json.dumps({**settings, "audit_log": "audit.jsonl"}))
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcpilot.gateway", "--config", str(config)],
                                   cwd=str(tmp_path))
    async with Client(params) as client:
        found = (await client.call_tool(FIND_TOOLS, {"task": "Przeczytaj lokalny plik README"})).structured_content
        assert found["connections"][0]["integration_id"] == "mcpilot/filesystem"
        reader = next(t for t in found["tools"] if t["name"] == "read_file")
        result = await client.call_tool(CALL_TOOL, {"tool_id": reader["id"], "arguments": {"path": "README.md"}})
        assert "Project Atlas" in json.dumps(result.structured_content)
        # Named providers without credentials report a user action, never a URL or secret.
        github = (await client.call_tool(FIND_TOOLS, {"task": "Read GitHub issues"})).structured_content
        assert github["needs_user"] == [{"integration_id": "com.github/remote", "account": "default",
                                         "action": "connect_account"}]
    events = [json.loads(line) for line in (tmp_path / "gateway" / "audit.jsonl").read_text().splitlines()]
    assert [(e["action"], e["outcome"]) for e in events][:2] == [("connect", "ready"), ("call", "ok")]
    assert all(e["user_id"] == "local-user" for e in events)
