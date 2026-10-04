"""Opt-in checks against real package registries: MCPILOT_NETWORK_TESTS=1 pytest tests/test_network.py"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from mcpilot import MCPilot, Policy
from mcpilot.catalog import Catalog
from mcpilot.integrations import reference_filesystem

pytestmark = pytest.mark.skipif(os.environ.get("MCPILOT_NETWORK_TESTS") != "1",
                                reason="network test; set MCPILOT_NETWORK_TESTS=1")
WORKSPACE = Path(__file__).parent.parent / "examples" / "workspace"


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is required")
async def test_official_filesystem_server_installs_pinned_and_reads(tmp_path):
    integration = reference_filesystem(WORKSPACE)
    policy = Policy([integration], capabilities=["files.read"])
    async with MCPilot(user_id="alice", catalog=Catalog([integration]), policy=policy, state_dir=tmp_path) as pilot:
        toolset = await pilot.tools_for("Przeczytaj lokalny plik README")
        assert toolset.connections[0].status == "ready", toolset.connections
        names = {t.name for t in toolset.tools}
        assert "read_text_file" in names and "write_file" not in names
        reader = next(t for t in toolset.tools if t.name == "read_text_file")
        result = await pilot.call(reader.id, {"path": str(WORKSPACE.resolve() / "README.md")})
        assert "Project Atlas" in result.content[0]["text"]
    lock = next((tmp_path / "runtimes" / "packages").glob("*/package-lock.json"))
    assert '"@modelcontextprotocol/server-filesystem": "2026.8.31"' in lock.read_text()
