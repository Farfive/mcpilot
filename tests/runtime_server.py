"""Real MCP fixture, runnable as a subprocess over either supported transport."""

import asyncio
import os
import sys

from mcp.server import MCPServer

server = MCPServer("MCPilot runtime acceptance fixture", log_level="CRITICAL")
calls = 0


@server.tool()
def add(left: int, right: int) -> dict[str, int]:
    """Add two integers."""
    return {"sum": left + right}


@server.tool()
def process_info() -> dict[str, str | int | bool]:
    """Inspect fixture identity and check credential isolation."""
    return {
        "pid": os.getpid(),
        "home": os.environ.get("HOME", ""),
        "ambient_secret_present": "MCPILOT_TEST_AMBIENT_SECRET" in os.environ,
        "explicit_secret_present": "MCPILOT_TEST_EXPLICIT_SECRET" in os.environ,
    }


@server.tool()
async def slow_write(delay: float) -> dict[str, int]:
    """Count a write before a delay so timeout tests detect unsafe replay."""
    global calls
    calls += 1
    await asyncio.sleep(delay)
    return {"calls": calls}


@server.tool()
def write_count() -> dict[str, int]:
    return {"calls": calls}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "http":
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run("stdio")
