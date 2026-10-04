/**
 * Task -> plan -> local stdio MCP server -> tool call, from TypeScript.
 * Run from ts/: npm run build && node dist/examples/local-task.js
 * Uses the repository's read-only Python filesystem server (needs ../.venv).
 */

import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { Catalog, MCPilot, parseIntegration, Policy, Runtime } from "../src/index.js";

const root = join(fileURLToPath(import.meta.url), "..", "..", "..", "..");
const python = process.env.MCPILOT_PYTHON ?? join(root, ".venv", "bin", "python");
const workspace = join(root, "examples", "workspace");

const integration = parseIntegration({
  id: "mcpilot/filesystem", service: "filesystem", publisher: "MCPilot", version: "0.1.0", transport: "stdio",
  command: [python, "-m", "mcpilot.servers.filesystem", workspace], capabilities: ["files.read"], support: "supported",
  tools: { read_file: { capability: "files.read" }, list_files: { capability: "files.read" } },
});

const pilot = new MCPilot({
  userId: "demo-user",
  catalog: new Catalog([integration]),
  policy: new Policy([integration], { capabilities: ["files.read"] }),
  runtime: new Runtime({ directory: join(root, ".mcpilot", "ts-runtime") }),
});

try {
  const plan = await pilot.plan("Przeczytaj lokalny plik README projektu");
  console.log("Plan:", plan.selections.map((s) => [s.integration_id, s.capability]));
  const toolset = await pilot.toolsFor(plan);
  console.log("Connection:", toolset.connections.map((c) => c.status));
  const reader = toolset.tools.find((t) => t.name === "read_file")!;
  const result = await pilot.call(reader.id, { path: "README.md" });
  console.log("Result:", result.structured_content ?? result.content);
} finally {
  await pilot.close();
}
