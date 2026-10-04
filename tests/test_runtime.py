from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcpilot.auth import AuthRequired
from mcpilot.installer import InstallationError, PackageInstaller
from mcpilot.models import Integration, PackageSpec
from mcpilot.runtime import Runtime, RuntimeFailure, RuntimeTimeout, _Request, _sanitized_error

FIXTURE = Path(__file__).with_name("runtime_server.py")


def stdio_integration() -> Integration:
    return Integration(
        id="test.runtime.stdio", service="fixture", publisher="test", version="1.0.0",
        transport="stdio", command=(sys.executable, str(FIXTURE)),
    )


@pytest.mark.asyncio
async def test_stdio_real_tools_environment_and_cross_task_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPILOT_TEST_AMBIENT_SECRET", "never-forward")
    runtime = Runtime(tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()
    session = await asyncio.create_task(runtime.open(
        stdio_integration(), env={"MCPILOT_TEST_EXPLICIT_SECRET": "host-approved"}
    ))
    tools = await session.list_tools()
    assert {tool["name"] for tool in tools} >= {"add", "process_info"}
    assert next(tool for tool in tools if tool["name"] == "add")["input_schema"]["type"] == "object"
    result = await session.call_tool("add", {"left": 2, "right": 4})
    assert result["structured_content"] == {"sum": 6}
    assert result["is_error"] is False
    info = (await session.call_tool("process_info", {}))["structured_content"]
    assert not info["ambient_secret_present"]
    assert info["explicit_secret_present"]
    assert str(tmp_path) in info["home"]
    await asyncio.create_task(session.ping())
    await asyncio.create_task(runtime.close())
    assert session.closed
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(info["pid"], 0)
    with pytest.raises(RuntimeFailure, match="closed"):
        await session.list_tools()


@pytest.mark.asyncio
async def test_stdio_timeout_never_replays_write(tmp_path):
    async with Runtime(tmp_path / "runtime", timeout=3) as runtime:
        session = await runtime.open(stdio_integration())
        await session.list_tools()
        session.timeout = 0.15
        with pytest.raises(RuntimeTimeout, match="unknown"):
            await session.call_tool("slow_write", {"delay": 0.5})
        session.timeout = 3
        result = await session.call_tool("write_count", {})
        assert result["structured_content"] == {"calls": 1}


@pytest.mark.asyncio
async def test_http_real_streamable_connection(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(FIXTURE), "http", str(port),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15):
            while True:
                if process.returncode is not None:
                    pytest.fail("HTTP fixture stopped before accepting connections")
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
        integration = Integration(
            id="test.runtime.http", service="fixture", publisher="test", version="1.0.0",
            transport="http", endpoint=f"http://127.0.0.1:{port}/mcp",
        )
        async with Runtime(tmp_path / "runtime") as runtime:
            session = await runtime.open(integration)
            await session.ping()
            assert await session.list_tools()
            result = await session.call_tool("add", {"left": 8, "right": 3})
            assert result["structured_content"] == {"sum": 11}
        assert not (tmp_path / "runtime" / "packages").exists()
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


@pytest.mark.asyncio
async def test_tool_listing_is_bounded(tmp_path):
    async with Runtime(tmp_path / "runtime", max_tools=1) as runtime:
        session = await runtime.open(stdio_integration())
        with pytest.raises(RuntimeFailure, match="limit"):
            await session.list_tools()


@pytest.mark.asyncio
async def test_pagination_cycles_are_rejected(tmp_path):
    async with Runtime(tmp_path / "runtime") as runtime:
        session = await runtime.open(stdio_integration())

        class RepeatedPage:
            async def list_tools(self, *, cursor=None):
                return SimpleNamespace(tools=[], next_cursor="same-page")

        request = _Request("list_tools", (), asyncio.get_running_loop().create_future())
        with pytest.raises(RuntimeFailure, match="repeated"):
            await session._execute(RepeatedPage(), request)


@pytest.mark.parametrize("spec", [
    PackageSpec(runtime="python", name="safe", version=">=1", entrypoint="safe"),
    PackageSpec(runtime="python", name="https://evil/package", version="1.0", entrypoint="safe"),
    PackageSpec(runtime="python", name="safe", version="1.0", entrypoint="../evil"),
    PackageSpec(runtime="python", name="safe", version="1.0", entrypoint="safe", dependencies=("requests>=2",)),
    PackageSpec(runtime="python", name="safe", version="1.0"),
    PackageSpec(runtime="node", name="safe", version="latest", entrypoint="safe"),
    PackageSpec(runtime="node", name="safe", version="1.0.0", entrypoint="safe", dependencies=("other@^2",)),
])
@pytest.mark.asyncio
async def test_installer_rejects_unpinned_or_executable_injection_without_io(tmp_path, spec):
    installer = PackageInstaller(tmp_path / "packages")
    with pytest.raises(InstallationError):
        await installer.prepare(spec)
    assert not (tmp_path / "packages").exists()


@pytest.mark.asyncio
async def test_installer_concurrent_prepare_installs_once_and_reuses(tmp_path, monkeypatch):
    installer = PackageInstaller(tmp_path / "packages")
    calls = []

    async def fake_run(command, directory):
        calls.append(command)
        if "venv" in command:
            binary = directory / ("Scripts" if os.name == "nt" else "bin")
            binary.mkdir()
            (binary / "approved-server").write_text("fixture")
        await asyncio.sleep(0)

    monkeypatch.setattr(installer, "_run", fake_run)
    spec = PackageSpec(runtime="python", name="approved", version="1.2.3", entrypoint="approved-server")
    first, second = await asyncio.gather(installer.prepare(spec), installer.prepare(spec))
    assert first == second
    assert len(calls) == 2
    assert "--no-deps" in calls[1]
    assert "--only-binary=:all:" in calls[1]
    assert "approved==1.2.3" in calls[1]
    assert await installer.prepare(spec) == first
    assert len(calls) == 2


def test_safe_auth_states_survive_anyio_groups_but_remote_errors_do_not():
    pending = AuthRequired("Connection requires host login")
    assert _sanitized_error(ExceptionGroup("remote-token-secret", [pending])) is pending
    assert "remote-token-secret" not in str(_sanitized_error(ValueError("remote-token-secret")))
