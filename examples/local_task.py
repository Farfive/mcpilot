"""Run: python examples/local_task.py (after pip install -e .)."""

import asyncio
from pathlib import Path

from mcpilot import MCPilot, Policy
from mcpilot.catalog import Catalog
from mcpilot.integrations import filesystem


async def main() -> None:
    integration = filesystem(Path(__file__).parent / "workspace")
    async with MCPilot(user_id="demo-user", catalog=Catalog([integration]),
                       policy=Policy([integration], capabilities=["files.read"])) as pilot:
        plan = await pilot.plan("Przeczytaj lokalny plik README projektu")
        selected = await pilot.tools_for(plan)
        print("Plan:", [(s.integration_id, s.capability) for s in plan.selections])
        print("Connection:", [c.status for c in selected.connections])
        tool = next(t for t in selected.tools if t.name == "read_file")
        result = await pilot.call(tool.id, {"path": "README.md"})
        print("Result:", result.structured_content or result.content)


if __name__ == "__main__":
    asyncio.run(main())
