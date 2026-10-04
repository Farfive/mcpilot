"""pip install -e '.[langchain]' then python examples/langchain_tools.py."""

import asyncio
from pathlib import Path

from mcpilot import MCPilot, Policy
from mcpilot.adapters.langchain import as_langchain_tools
from mcpilot.catalog import Catalog
from mcpilot.integrations import filesystem


async def main():
    integration = filesystem(Path(__file__).parent / "workspace")
    async with MCPilot(user_id="demo", catalog=Catalog([integration]),
                       policy=Policy([integration], capabilities=["files.read"])) as pilot:
        selected = await pilot.tools_for("Read the local README file")
        tools = as_langchain_tools(pilot, selected)
        reader_id = next(t.id for t in selected.tools if t.name == "read_file")
        reader = next(t for t in tools if t.name == reader_id)
        print(await reader.ainvoke({"path": "README.md"}))


if __name__ == "__main__":
    asyncio.run(main())
