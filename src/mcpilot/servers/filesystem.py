"""Read-only reference adapter scoped to one host-selected directory."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from mcp.server import MCPServer


def create_server(root: Path) -> MCPServer:
    root = root.resolve(strict=True)
    server = MCPServer("MCPilot read-only filesystem", version="0.1.0")

    def resolve(path: str) -> Path:
        candidate = (root / path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("Path is outside the allowed directory")
        return candidate

    @server.tool()
    def read_file(path: str) -> str:
        """Read a UTF-8 text file relative to the allowed directory (maximum 64 KiB)."""
        target = resolve(path)
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Only regular files are supported")
            content = stream.read(65_537)
        if len(content) > 65_536:
            raise ValueError("File is too large; select a smaller document")
        return content.decode("utf-8")

    @server.tool()
    def list_files(path: str = ".") -> list[str]:
        """List at most 200 direct entries in an allowed directory."""
        target = resolve(path)
        return sorted(p.name for p in target.iterdir())[:200]

    return server


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m mcpilot.servers.filesystem DIRECTORY")
    create_server(Path(sys.argv[1])).run(transport="stdio")


if __name__ == "__main__":
    main()
