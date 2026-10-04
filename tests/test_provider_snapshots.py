"""TECH-3: integration templates must match captured provider tool lists (spec/providers)."""

import json
from pathlib import Path

from examples.notion_github_task import steps_for
from mcpilot.integrations import github, notion

PROVIDERS = Path(__file__).resolve().parent.parent / "spec" / "providers"


def snapshot(name):
    return json.loads((PROVIDERS / name).read_text())


def test_github_template_maps_only_tools_of_the_real_server():
    tools = snapshot("github-mcp-server-1.14.0.json")["tools"]
    assert set(github().tools) <= set(tools)
    # The flagship step must satisfy the real issue_read schema (method is required upstream).
    step = next(s for s in steps_for({"com.github/remote", "com.notion/mcp"}) if s.tool_name == "issue_read")
    assert set(tools["issue_read"]["required"]) <= set(step.arguments)
    assert set(step.arguments) <= set(tools["issue_read"]["properties"])


def test_notion_template_uses_documented_read_only_tools():
    tools = snapshot("notion-mcp-docs-2026-10-05.json")["tools"]
    assert set(notion().tools) <= set(tools)
    assert all(tools[name]["read_only"] for name in notion().tools)
