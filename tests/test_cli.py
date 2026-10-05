"""Pilot kit: setup → two gateway sessions without re-login → status → disconnect → uninstall."""

import asyncio
import json
import os
import sys
from pathlib import Path

import keyring
import pytest
from keyring_file_backend import FileKeyring
from mcp import Client, StdioServerParameters

from examples.notion_github_task import GITHUB_PAT, github_fixture
from mcpilot.auth import MemorySecretStore
from mcpilot.cli import main
from mcpilot.discovery import CALL_TOOL, FIND_TOOLS
from mcpilot.gateway import GatewayConfig, open_store

TESTS = Path(__file__).resolve().parent
WORKSPACE = TESTS.parent / "examples" / "workspace"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """File-backed keyring and a fake `claude` CLI that records its arguments."""
    monkeypatch.setenv("MCPILOT_TEST_KEYRING", str(tmp_path / "keyring.json"))
    monkeypatch.delenv("MCPILOT_SECRET_KEY", raising=False)
    previous = keyring.get_keyring()
    keyring.set_keyring(FileKeyring())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude.log"
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n[ "$1 $2" = "mcp get" ] && exit "${{FAKE_GET_STATUS:-1}}"\nexit 0\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    yield tmp_path, log
    keyring.set_keyring(previous)


def gateway_params(config: Path) -> StdioServerParameters:
    env = {**os.environ, "PYTHON_KEYRING_BACKEND": "keyring_file_backend.FileKeyring",
           "PYTHONPATH": os.pathsep.join([str(TESTS), os.environ.get("PYTHONPATH", "")])}
    return StdioServerParameters(command=sys.executable, args=["-m", "mcpilot.gateway", "--config", str(config)], env=env)


async def github_session(config: Path) -> dict:
    async with Client(gateway_params(config)) as client:
        found = (await client.call_tool(FIND_TOOLS, {"task": "Read GitHub issues"})).structured_content
        if found["tools"]:
            tool = next(t for t in found["tools"] if t["name"] == "search_issues")
            result = await client.call_tool(CALL_TOOL, {"tool_id": tool["id"], "arguments": {"query": "repo:acme/atlas"}})
            found["call"] = result.structured_content
        return found


def test_pilot_kit_end_to_end_without_secrets_in_host_config(isolated, monkeypatch, capsys):
    tmp_path, log = isolated
    directory = tmp_path / "pilot"
    with github_fixture().serve() as fixture:
        monkeypatch.setenv("PILOT_PAT", GITHUB_PAT)
        assert main(["setup", "--dir", str(directory), "--user", "pilot", "--no-notion", "--workspace", str(WORKSPACE),
                     "--github-endpoint", fixture.endpoint, "--allow-loopback", "--github-pat-env", "PILOT_PAT",
                     "--register", "claude-code", "--scope", "local", "--no-registry-sync"]) == 0
        key = json.loads((tmp_path / "keyring.json").read_text())
        assert len(key) == 1 and next(iter(key)).startswith("mcpilot/gateway-")
        secret_key = next(iter(key.values()))
        registration = log.read_text()
        assert "mcp add --scope local mcpilot -- " in registration and "-m mcpilot.gateway --config" in registration
        written = registration + "".join(p.read_text(errors="ignore") for p in directory.rglob("*") if p.is_file())
        assert GITHUB_PAT not in written and secret_key not in written  # host config, manifest, store, setup.json

        # Two host sessions = two gateway processes: the stored PAT is reused, no login, no env secret.
        for _ in range(2):
            session = asyncio.run(github_session(directory / "gateway.json"))
            assert session["needs_user"] == [] and session["call"]["is_error"] is False
        assert len(fixture.calls) == 2

        capsys.readouterr()
        assert main(["status", "--config", str(directory / "gateway.json")]) == 0
        status = capsys.readouterr().out
        assert "pęk kluczy systemu" in status and "PAT/klucz zapisany" in status and "bez logowania" in status
        assert GITHUB_PAT not in status and secret_key not in status

        assert main(["disconnect", "github", "--config", str(directory / "gateway.json")]) == 0
        assert "Personal access tokens" in capsys.readouterr().out
        main(["status", "--config", str(directory / "gateway.json")])
        assert "brak połączenia" in capsys.readouterr().out
        session = asyncio.run(github_session(directory / "gateway.json"))
        assert session["needs_user"] == [{"integration_id": "com.github/remote", "account": "default",
                                          "action": "connect_account"}]

    assert main(["uninstall", "--dir", str(directory)]) == 0
    assert "mcp remove --scope local mcpilot" in log.read_text()
    assert json.loads((tmp_path / "keyring.json").read_text()) == {}


def test_setup_never_overwrites_an_existing_host_entry(isolated, monkeypatch, capsys):
    tmp_path, log = isolated
    monkeypatch.setenv("FAKE_GET_STATUS", "0")  # `claude mcp get mcpilot` succeeds: already registered
    assert main(["setup", "--dir", str(tmp_path / "pilot"), "--no-github", "--register", "claude-code"]) == 0
    assert "już istnieje" in capsys.readouterr().out
    assert "mcp add" not in log.read_text()


def test_without_any_key_the_gateway_warns_and_keeps_tokens_in_memory(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MCPILOT_SECRET_KEY", raising=False)
    (tmp_path / "m.json").write_text("[]")
    (tmp_path / "g.json").write_text(json.dumps({"user_id": "u", "manifest": "m.json", "capabilities": []}))
    store = open_store(GatewayConfig.load(tmp_path / "g.json"))
    assert isinstance(store, MemorySecretStore)
    assert "every restart needs a new login" in capsys.readouterr().err
