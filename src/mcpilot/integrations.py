"""Reviewed adapter templates. Hosts must approve an exact configured manifest."""

from __future__ import annotations

import sys
from pathlib import Path

from .models import AuthSpec, Integration, PackageSpec, ToolRule


def filesystem(root: str | Path) -> Integration:
    directory = Path(root).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("Filesystem root must be a directory")
    return Integration(
        id="mcpilot/filesystem", service="filesystem", publisher="MCPilot", version="0.1.0",
        transport="stdio", command=(sys.executable, "-m", "mcpilot.servers.filesystem", str(directory)),
        capabilities=("files.read",), support="supported",
        tools={"read_file": ToolRule(capability="files.read"),
               "list_files": ToolRule(capability="files.read")},
    )


def reference_filesystem(root: str | Path) -> Integration:
    """Official reference server; explicit connect installs an isolated pinned npm package."""
    directory = Path(root).resolve(strict=True)
    return Integration(
        id="io.modelcontextprotocol/filesystem", service="filesystem",
        publisher="Model Context Protocol", version="2026.8.31", transport="stdio",
        package=PackageSpec(runtime="node", name="@modelcontextprotocol/server-filesystem",
                            version="2026.8.31", entrypoint="mcp-server-filesystem", args=(str(directory),)),
        capabilities=("files.read",), support="experimental",
        tools={name: ToolRule(capability="files.read")
               for name in ("read_file", "read_text_file", "list_directory", "search_files")},
        source="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    )


def github(endpoint: str = "https://api.githubcopilot.com/mcp/readonly") -> Integration:
    """Read-only remote server; a PAT or host OAuth token is sent as a bearer credential."""
    return Integration(
        id="com.github/remote", service="github", publisher="GitHub", version="managed",
        transport="http", endpoint=endpoint,
        capabilities=("issues.read",), auth=AuthSpec(mode="bearer"), support="experimental",
        tools={name: ToolRule(capability="issues.read") for name in ("search_issues", "issue_read", "list_issues")},
        source="https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md",
    )


def notion(endpoint: str = "https://mcp.notion.com/mcp") -> Integration:
    """Hosted server with interactive MCP OAuth (discovery, DCR, PKCE)."""
    return Integration(
        id="com.notion/mcp", service="notion", publisher="Notion", version="managed",
        transport="http", endpoint=endpoint, capabilities=("documents.search",),
        auth=AuthSpec(mode="oauth"), support="experimental",
        tools={name: ToolRule(capability="documents.search") for name in ("notion-search", "notion-fetch")},
        source="https://developers.notion.com/guides/mcp/mcp",
    )


def provider_candidates() -> tuple[Integration, ...]:
    """Drive/Slack are setup candidates until a host reviews and configures tool mappings."""
    return (
        github(), notion(),
        Integration(id="com.google/drive", service="google-drive", publisher="Google",
                    version="preview", transport="http", endpoint="https://drivemcp.googleapis.com/mcp/v1",
                    capabilities=("documents.search",), support="discovered",
                    auth=AuthSpec(mode="oauth", setup_required="Workspace Developer Preview, Cloud project, APIs and OAuth client required",
                                  scopes_by_capability={"documents.search": ("https://www.googleapis.com/auth/drive.readonly",)}),
                    source="https://developers.google.com/workspace/drive/mcp"),
        Integration(id="com.slack/mcp", service="slack", publisher="Slack", version="managed",
                    transport="http", endpoint="https://mcp.slack.com/mcp",
                    capabilities=("messages.search", "messages.send"), support="discovered",
                    auth=AuthSpec(mode="oauth", setup_required="Registered Slack application and workspace approval required; no dynamic registration"),
                    source="https://docs.slack.dev/ai/slack-mcp-server/"),
    )
