"""Registry entry → human approval → read-only manifest → gateway picks it up without a restart."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx2 as httpx
import pytest

from mcpilot.approval import (
    ApprovalError,
    approve,
    classify,
    draft_package,
    draft_remote,
    remove,
    service_name,
)
from mcpilot.auth import AuthManager, LoginBroker, MemorySecretStore, OAuthConfig
from mcpilot.catalog import Catalog
from mcpilot.cli import main
from mcpilot.discovery import CALL_TOOL, FIND_TOOLS, DiscoveryTools
from mcpilot.gateway import ApprovalReloader, GatewayConfig
from mcpilot.policy import Policy, fingerprint
from mcpilot.sdk import MCPilot
from mcpilot.search import CapabilityIndex

FIXTURE = Path(__file__).with_name("annotated_server.py")
REGISTRY = "https://registry.test/v0.1/servers"


def answers(*replies: str):
    pending = iter(replies)
    return lambda _question: next(pending)


def entry(name: str, **server) -> dict:
    return {"server": {"name": name, "version": "2.0.0", "description": "Pages and search", **server},
            "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}}}


def registry_client(entries: dict[str, dict], npm: dict | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "registry.npmjs.org":
            return httpx.Response(200, json=npm or {})
        for name, value in entries.items():
            if request.url.raw_path.decode().endswith(f"/{name.replace('/', '%2F')}/versions/latest"):
                return httpx.Response(200, json=value)
        return httpx.Response(404, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
async def annotated_server():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = await asyncio.create_subprocess_exec(sys.executable, str(FIXTURE), str(port),
                                                   stdout=asyncio.subprocess.DEVNULL,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(15):
            while True:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        await process.wait()


def gateway_dir(tmp_path: Path) -> Path:
    (tmp_path / "approved-integrations.json").write_text(json.dumps({"integrations": []}))
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps({"user_id": "ana", "manifest": "approved-integrations.json",
                                  "capabilities": [], "state_dir": "state", "allow_loopback": True}))
    return config


def test_service_names():
    assert service_name("com.atlassian/atlassian-mcp-server") == "atlassian"
    assert service_name("com.stripe/mcp") == "stripe"
    assert service_name("io.github.jdoe/jira-mcp") == "jira"
    assert service_name("io.github.github/github-mcp-server") == "github"


def test_remote_draft_auth_modes_and_refusals():
    oauth = draft_remote(entry("com.acme/pages", remotes=[{"type": "streamable-http", "url": "https://mcp.acme.com/mcp"}]))
    assert oauth.integration.auth.mode == "oauth" and oauth.integration.version == "2.0.0"
    assert oauth.integration.tools == {} and oauth.official
    header = {"name": "Authorization", "isSecret": True, "isRequired": True, "description": "PAT"}
    token = draft_remote(entry("com.acme/pages", remotes=[{"type": "streamable-http", "url": "https://x.acme.com/mcp",
                                                            "headers": [header]}]))
    assert token.integration.auth.mode == "bearer" and token.secret_label.startswith("Authorization")
    optional = draft_remote(entry("com.acme/pages", remotes=[{"type": "streamable-http", "url": "https://x.acme.com/mcp",
                                                               "headers": [{**header, "isRequired": False}]}]))
    assert optional.integration.auth.mode == "oauth" and "--auth token" in optional.warnings[0]
    with pytest.raises(ApprovalError, match="template"):
        draft_remote(entry("com.acme/pages", remotes=[{"type": "streamable-http", "url": "https://{tenant}.acme.com/mcp"}]))
    with pytest.raises(ApprovalError, match="Streamable HTTP"):
        draft_remote(entry("com.acme/pages", remotes=[{"type": "sse", "url": "https://acme.com/sse"}]))
    with pytest.raises(ApprovalError, match="without a value"):
        draft_remote(entry("com.acme/pages", remotes=[{"type": "streamable-http", "url": "https://acme.com/mcp",
                                                        "headers": [{"name": "X-Org", "isRequired": True}]}]))


async def test_package_drafts_local_and_container(monkeypatch):
    npm = entry("io.github.jdoe/pages-mcp", packages=[{
        "registryType": "npm", "identifier": "@jdoe/pages-mcp", "version": "1.4.2", "transport": {"type": "stdio"},
        "environmentVariables": [{"name": "PAGES_TOKEN", "isSecret": True, "isRequired": True}]}])
    async with registry_client({}, npm={"bin": {"pages-mcp": "dist/index.js"}}) as client:
        local = await draft_package(npm, container=False, client=client)
    assert local.kind == "local" and not local.official
    assert local.integration.package.entrypoint == "pages-mcp" and local.integration.package.version == "1.4.2"
    assert local.integration.auth.env_var == "PAGES_TOKEN" and "runs code" in local.warnings[0]

    monkeypatch.setattr("mcpilot.approval.shutil.which", lambda _: "/usr/bin/docker")
    boxed = await draft_package(npm, container=True)
    assert boxed.kind == "container"
    command = boxed.integration.command
    assert command[:4] == ("/usr/bin/docker", "run", "-i", "--rm") and "--cap-drop" in command
    assert ("-e", "PAGES_TOKEN") == command[command.index("-e"):command.index("-e") + 2]
    assert command[-1] == "@jdoe/pages-mcp@1.4.2"

    oci = entry("io.github.jdoe/pages", packages=[{"registryType": "oci", "identifier": "ghcr.io/jdoe/pages:1.0",
                                                    "transport": {"type": "stdio"}}])
    with pytest.raises(ApprovalError, match="--container"):
        await draft_package(oci, container=False)
    assert (await draft_package(oci, container=True)).integration.command[-1] == "ghcr.io/jdoe/pages:1.0"
    unpinned = entry("io.github.jdoe/pages", packages=[{"registryType": "oci", "identifier": "ghcr.io/jdoe/pages",
                                                         "transport": {"type": "stdio"}}])
    with pytest.raises(ApprovalError, match="pinned"):
        await draft_package(unpinned, container=True)
    pypi = entry("io.github.jdoe/pages", packages=[{"registryType": "pypi", "identifier": "pages-mcp",
                                                     "version": "0.3.0", "transport": {"type": "stdio"}}])
    with pytest.raises(ApprovalError, match="--container"):
        await draft_package(pypi, container=False)


def test_classify_maps_only_read_only_hints_by_default():
    tools = [{"name": "a", "annotations": {"read_only_hint": True}},
             {"name": "b", "annotations": {"read_only_hint": False}},
             {"name": "c", "annotations": {}}]
    rules, excluded = classify(tools, "acme", include_write=False)
    assert {n: (r.capability, r.effect) for n, r in rules.items()} == {"a": ("acme.read", "read")}
    assert excluded == ["b", "c"]
    rules, excluded = classify(tools, "acme", include_write=True)
    assert rules["c"].effect == "write" and rules["c"].capability == "acme.write" and not excluded


def broker_that_must_not_open() -> OAuthConfig:
    async def show(_request) -> None:
        raise AssertionError("a public server must not trigger a login")
    return OAuthConfig(login=LoginBroker(show, timeout=5), redirect_uri="http://127.0.0.1:1/callback")


async def test_approve_end_to_end_then_gateway_reloads_without_restart(tmp_path, annotated_server):
    config_path = gateway_dir(tmp_path)
    registry = {"com.acme/pages": entry("com.acme/pages", remotes=[{"type": "streamable-http", "url": annotated_server}])}
    store = MemorySecretStore()

    # A running gateway session with no approvals yet.
    config = GatewayConfig.load(config_path)
    catalog = Catalog()
    catalog.load_manifest(config.manifest)
    policy = Policy([], capabilities=[], allow_loopback=True)
    async with MCPilot(user_id="ana", catalog=catalog, policy=policy, auth=AuthManager(store),
                       state_dir=tmp_path / "state") as pilot:
        tools = DiscoveryTools(pilot, index=CapabilityIndex.from_catalog(catalog))
        reload = ApprovalReloader(config_path, config, pilot, tools)
        assert not (await tools.handle(FIND_TOOLS, {"task": "search pages", "services": ["acme"]}))["tools"]

        # The person declines: nothing is written.
        out: list[str] = []
        async with registry_client(registry) as client:
            declined = await approve("com.acme/pages", config_path=config_path, store=store, user_id="ana",
                                     ask=answers(*["t", "n"]), say=out.append, login=broker_that_must_not_open(),
                                     state_dir=tmp_path / "state", allow_loopback=True, source=REGISTRY, client=client)
        assert declined is None
        assert json.loads((tmp_path / "approved-integrations.json").read_text()) == {"integrations": []}
        assert any("[pominięte, zapis] create_page" in line for line in out)
        assert any("[pominięte, zapis] delete_page" in line for line in out)
        assert any("oficjalna przestrzeń nazw" in line for line in out)

        # The person approves: read-only tools only, pinned version, capability allowed.
        async with registry_client(registry) as client:
            record = await approve("com.acme/pages", config_path=config_path, store=store, user_id="ana",
                                   ask=answers(*["t", "t"]), say=out.append, login=broker_that_must_not_open(),
                                   state_dir=tmp_path / "state", allow_loopback=True, source=REGISTRY, client=client)
        assert record["tools"] == ["get_page", "search_pages"] and record["capabilities"] == ["acme.read"]
        manifest = json.loads((tmp_path / "approved-integrations.json").read_text())["integrations"]
        assert manifest[0]["version"] == "2.0.0" and manifest[0]["endpoint"] == annotated_server
        settings = json.loads(config_path.read_text())
        assert settings["capabilities"] == ["acme.read"] and "write" not in settings.get("effects", [])
        log = [json.loads(line) for line in (tmp_path / "approvals.jsonl").read_text().splitlines()]
        assert log[-1]["fingerprint"] == record["fingerprint"]

        # The running gateway picks the approval up on the next search.
        assert await reload()
        assert fingerprint(pilot.catalog.get("com.acme/pages")) == record["fingerprint"]
        found = await tools.handle(FIND_TOOLS, {"task": "search pages in acme"})
        assert sorted(t["name"] for t in found["tools"]) == ["get_page", "search_pages"], found
        assert all(t["effect"] == "read" for t in found["tools"])
        search = next(t for t in found["tools"] if t["name"] == "search_pages")
        result = await tools.handle(CALL_TOOL, {"tool_id": search["id"], "arguments": {"query": "road"}})
        assert result["structured_content"] == {"titles": ["roadmap"]}
        assert not await reload()  # unchanged files: no work

        # Withdrawal: the tools disappear and issued ids stop working.
        assert remove(config_path, "com.acme/pages")
        assert await reload()
        denied = await tools.handle(CALL_TOOL, {"tool_id": search["id"], "arguments": {"query": "road"}})
        assert denied["error"] == "PolicyDenied"
        assert not (await tools.handle(FIND_TOOLS, {"task": "search pages", "services": ["acme"]}))["tools"]


async def test_write_tools_need_a_second_typed_confirmation(tmp_path, annotated_server):
    config_path = gateway_dir(tmp_path)
    registry = {"com.acme/pages": entry("com.acme/pages", remotes=[{"type": "streamable-http", "url": annotated_server}])}
    common = dict(config_path=config_path, store=MemorySecretStore(), user_id="ana", say=lambda _: None,
                  login=broker_that_must_not_open(), include_write=True, state_dir=tmp_path / "state",
                  allow_loopback=True, source=REGISTRY)
    async with registry_client(registry) as client:
        assert await approve("com.acme/pages", ask=answers(*["t", "t", "tak"]), client=client, **common) is None
        record = await approve("com.acme/pages", ask=answers(*["t", "t", "zapis"]), client=client, **common)
    assert "create_page" in record["tools"] and "acme.write" in record["capabilities"]
    assert "write" in json.loads(config_path.read_text())["effects"]


async def test_local_package_requires_flag_and_typed_consent(tmp_path):
    config_path = gateway_dir(tmp_path)
    local_only = entry("io.github.jdoe/pages-mcp", packages=[
        {"registryType": "npm", "identifier": "@jdoe/pages-mcp", "version": "1.4.2", "transport": {"type": "stdio"}}])
    common = dict(config_path=config_path, store=MemorySecretStore(), user_id="ana",
                  state_dir=tmp_path / "state", source=REGISTRY)
    async with registry_client({"io.github.jdoe/pages-mcp": local_only}, npm={"bin": "dist/cli.js"}) as client:
        with pytest.raises(ApprovalError, match="--local"):
            await approve("io.github.jdoe/pages-mcp", ask=lambda _: "t", say=lambda _: None, client=client, **common)
        out: list[str] = []
        result = await approve("io.github.jdoe/pages-mcp", ask=lambda _: "t", say=out.append, local=True,
                               client=client, **common)
    assert result is None  # "t" is not the typed word: nothing ran, nothing saved
    assert any("To uruchomi kod na Twoim komputerze" in line for line in out)
    assert any("NIE oficjalny" in line for line in out)
    assert not (tmp_path / "state" / "runtime").exists()


async def test_deleted_and_unknown_entries_are_refused(tmp_path):
    config_path = gateway_dir(tmp_path)
    gone = entry("com.acme/gone", remotes=[{"type": "streamable-http", "url": "https://acme.com/mcp"}])
    gone["_meta"]["io.modelcontextprotocol.registry/official"]["status"] = "deleted"
    common = dict(config_path=config_path, store=MemorySecretStore(), user_id="ana", ask=lambda _: "t",
                  say=lambda _: None, state_dir=tmp_path / "state", source=REGISTRY)
    async with registry_client({"com.acme/gone": gone}) as client:
        with pytest.raises(ApprovalError, match="deleted"):
            await approve("com.acme/gone", client=client, **common)
        with pytest.raises(ApprovalError, match="no server"):
            await approve("com.acme/missing", client=client, **common)
    with pytest.raises(ApprovalError, match="look like"):
        await approve("not a name", **common)


def test_cli_approve_refuses_without_a_terminal(tmp_path, capsys):
    config_path = gateway_dir(tmp_path)
    # pytest's captured stdin/stdout are not TTYs, like a model running the command in the background.
    assert main(["approve", "com.acme/pages", "--config", str(config_path)]) == 2
    assert "terminala" in capsys.readouterr().err
    assert json.loads((tmp_path / "approved-integrations.json").read_text()) == {"integrations": []}


async def test_oauth_server_logs_in_once_during_approval_and_keeps_oauth(tmp_path):
    from examples.oauth_fixture import FixtureTool, OAuthFixture

    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    fixture_tools = {
        "documents_search": FixtureTool("Search", schema, lambda _: "found", {"readOnlyHint": True}),
        "documents_delete": FixtureTool("Delete", schema, lambda _: "deleted", {"destructiveHint": True}),
    }
    config_path = gateway_dir(tmp_path)
    store = MemorySecretStore()
    with OAuthFixture(tools=fixture_tools, expected_scope=None).serve() as fixture:
        logins: list[str] = []

        async def browser(request) -> None:  # stands in for the user's browser
            logins.append(request.integration_id)
            await fixture.redirect(request.url)

        registry = {"com.fixture/docs": entry("com.fixture/docs", remotes=[
            {"type": "streamable-http", "url": fixture.endpoint}])}
        async with registry_client(registry) as client:
            record = await approve(
                "com.fixture/docs", config_path=config_path, store=store, user_id="ana", ask=answers("t", "t"),
                say=lambda _: None, login=OAuthConfig(authorization_handler=browser, callback_handler=fixture.callback),
                state_dir=tmp_path / "state", allow_loopback=True, source=REGISTRY, client=client)
        assert logins == ["com.fixture/docs"] and record["tools"] == ["documents_search"]
        manifest = json.loads((tmp_path / "approved-integrations.json").read_text())["integrations"]
        assert manifest[0]["auth"]["mode"] == "oauth"

        # The gateway reuses the grant from the approval: no second login.
        config = GatewayConfig.load(config_path)
        catalog = Catalog()
        integrations = catalog.load_manifest(config.manifest)
        policy = Policy(integrations, capabilities=config.capabilities, allow_loopback=True)
        async with MCPilot(user_id="ana", catalog=catalog, policy=policy, auth=AuthManager(store),
                           state_dir=tmp_path / "state") as pilot:
            found = await DiscoveryTools(pilot).handle(FIND_TOOLS, {"task": "x", "services": ["fixture"]})
            assert [t["name"] for t in found["tools"]] == ["documents_search"]
        assert logins == ["com.fixture/docs"]
