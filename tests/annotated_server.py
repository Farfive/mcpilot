"""MCP fixture whose tools carry annotations, like a real vendor server listed in the registry."""

import sys

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("Annotated fixture", log_level="CRITICAL")
pages = {"roadmap": "Q4 roadmap: ship approvals"}


@server.tool(name="search_pages", annotations=ToolAnnotations(readOnlyHint=True))
def search_pages(query: str) -> dict[str, list[str]]:
    """Search pages by title."""
    return {"titles": [title for title in pages if query.lower() in title]}


@server.tool(name="get_page", annotations=ToolAnnotations(readOnlyHint=True))
def get_page(title: str) -> dict[str, str]:
    """Read one page."""
    return {"text": pages.get(title, "")}


@server.tool(name="create_page", annotations=ToolAnnotations(destructiveHint=False))
def create_page(title: str, text: str) -> dict[str, bool]:
    """Create a page."""
    pages[title] = text
    return {"created": True}


@server.tool(name="delete_page")
def delete_page(title: str) -> dict[str, bool]:
    """Delete a page (no annotations at all)."""
    return {"deleted": pages.pop(title, None) is not None}


if __name__ == "__main__":
    server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
