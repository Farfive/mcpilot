"""Write a local MCP gateway configuration and print host registration commands.

Run: python examples/gateway_setup.py [target-directory]

The filesystem integration works end-to-end locally. GitHub (PAT in the
GITHUB_PAT environment variable) and Notion (browser OAuth) require accounts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcpilot.integrations import filesystem, github, notion


def write_config(target: Path, workspace: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    manifest = target / "approved-integrations.json"
    integrations = [filesystem(workspace), github(), notion()]
    manifest.write_text(json.dumps({"integrations": [i.model_dump(mode="json") for i in integrations]}, indent=2))
    config = target / "gateway.json"
    config.write_text(json.dumps({
        "user_id": "local-user",
        "manifest": manifest.name,
        "capabilities": ["files.read", "issues.read", "documents.search"],
        "effects": ["read", "draft"],
        "state_dir": "state",
        "secrets": {"com.github/remote": "GITHUB_PAT"},
    }, indent=2))
    return config


def main() -> None:
    root = Path(__file__).resolve().parent
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root.parent / ".mcpilot" / "gateway"
    config = write_config(target, root / "workspace")
    command = [sys.executable, "-m", "mcpilot.gateway", "--config", str(config)]
    print(f"Konfiguracja: {config}\n")
    print("Claude Code:")
    print("  claude mcp add mcpilot -e MCPILOT_SECRET_KEY=... -e GITHUB_PAT=... -- " + " ".join(command) + "\n")
    env = {"MCPILOT_SECRET_KEY": "<klucz Fernet z menedżera sekretów>", "GITHUB_PAT": "<opcjonalnie>"}
    stdio = {"type": "stdio", "command": command[0], "args": command[1:], "env": env}
    print("Cursor (.cursor/mcp.json) i inne hosty z plikiem mcpServers:")
    print(json.dumps({"mcpServers": {"mcpilot": stdio}}, indent=2, ensure_ascii=False) + "\n")
    print("VS Code (.vscode/mcp.json):")
    print(json.dumps({"servers": {"mcpilot": stdio}}, indent=2, ensure_ascii=False))
    print("\nStatus hostów i sposób weryfikacji: docs/hosts.md")


if __name__ == "__main__":
    main()
