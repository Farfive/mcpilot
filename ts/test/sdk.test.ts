/** TypeScript SDK against real Python MCP servers over stdio and Streamable HTTP. */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { test } from "node:test";

import {
  AuthManager, BudgetExceeded, Catalog, DiscoveryTools, FIND_TOOLS, CALL_TOOL, InvalidArguments, MCPilot,
  MemoryWorkflowStore, parseIntegration, pinnedRequirements, Policy, PolicyDenied, Runtime, UncertainOutcome,
  WorkflowRunner, type AuditEvent, type Integration,
} from "../src/index.js";
import { JsonFileWorkflowStore } from "../src/node.js";
import { freePort, PYTHON, RUNTIME_SERVER, stop, tempDir, waitForPort } from "./helpers.js";

function calculator(changes: Record<string, unknown> = {}): Integration {
  return parseIntegration({
    id: "test/calculator", service: "calculator", publisher: "test", version: "1.0.0", transport: "stdio",
    command: [PYTHON, RUNTIME_SERVER], support: "supported", capabilities: ["math.add", "counter.write"],
    tools: { add: { capability: "math.add" }, process_info: { capability: "math.add" },
             slow_write: { capability: "counter.write", effect: "write" }, write_count: { capability: "counter.write" } },
    auth: { mode: "api_key", env_var: "MCPILOT_TEST_EXPLICIT_SECRET" },
    ...changes,
  });
}

async function pilotFor(integration: Integration, options: { capabilities?: string[]; effects?: ("read" | "write" | "draft" | "send")[];
  limits?: Record<string, number>; audit?: (e: AuditEvent) => void | Promise<void>; user?: string; auth?: AuthManager;
  tenant?: string } = {}) {
  const auth = options.auth ?? new AuthManager();
  const user = options.user ?? "alice";
  if (integration.auth.mode === "api_key") {
    await auth.setSecret(user, integration.id, "default", "host-approved", integration.endpoint ?? "stdio");
  }
  return new MCPilot({
    userId: user, catalog: new Catalog([integration]), auth,
    policy: new Policy([integration], { capabilities: options.capabilities ?? ["math.add"], effects: options.effects }),
    runtime: new Runtime({ directory: await tempDir(), timeoutMs: 5000 }), limits: options.limits, audit: options.audit,
    tenant: options.tenant,
  });
}

test("stdio: task -> plan -> tools -> call with mapped tools, validation and an isolated environment", async () => {
  process.env.MCPILOT_TEST_AMBIENT_SECRET = "never-forward";
  const events: AuditEvent[] = [];
  const pilot = await pilotFor(calculator(), { capabilities: ["math.add", "counter.write"], audit: (e) => { events.push(e); } });
  try {
    const plan = await pilot.plan({ task: "add", capabilities: ["math.add", "counter.write"] });
    const toolset = await pilot.toolsFor(plan);
    assert.deepEqual(toolset.connections.map((c) => c.status), ["ready"]);
    // slow_write is a write and the policy allows read/draft only; unmapped tools never appear.
    assert.deepEqual(toolset.tools.map((t) => t.name), ["add", "process_info", "write_count"]);
    const add = toolset.tools.find((t) => t.name === "add")!;
    assert.deepEqual((await pilot.call(add.id, { left: 2, right: 3 })).structured_content, { sum: 5 });
    const info = (await pilot.call(toolset.tools.find((t) => t.name === "process_info")!.id, {})).structured_content as Record<string, unknown>;
    assert.equal(info.ambient_secret_present, false);
    assert.equal(info.explicit_secret_present, true);
    await assert.rejects(pilot.call(add.id, { left: "two", right: 3 }), InvalidArguments);
    await assert.rejects(pilot.call("mcp_" + "0".repeat(32), {}), PolicyDenied);
    await pilot.disconnect("test/calculator");
    await assert.rejects(pilot.call(add.id, { left: 1, right: 1 }), PolicyDenied);
    assert.deepEqual(events.map((e) => [e.action, e.outcome]),
      [["connect", "ready"], ["call", "ok"], ["call", "ok"], ["call", "InvalidArguments"], ["call", "PolicyDenied"],
       ["disconnect", "ok"], ["call", "PolicyDenied"]]);
    assert.ok(!JSON.stringify(events).includes("two"));
  } finally {
    await pilot.close();
    delete process.env.MCPILOT_TEST_AMBIENT_SECRET;
  }
});

test("stdio: an uncertain write is never replayed; budgets and step limits hold", async () => {
  const pilot = await pilotFor(calculator(), { capabilities: ["counter.write", "math.add"], effects: ["read", "write"],
                                                limits: { max_steps: 3, tool_tokens: 4000 } });
  try {
    const small = await pilot.toolsFor({ task: "count", capabilities: ["counter.write"] }, { tokenBudget: 40 });
    assert.ok(small.truncated && small.tools.length === 0);
    const toolset = await pilot.toolsFor({ task: "count", capabilities: ["counter.write"] });
    const tools = Object.fromEntries(toolset.tools.map((t) => [t.name, t]));
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 300);
    await assert.rejects(pilot.call(tools.slow_write.id, { delay: 1 }, { signal: controller.signal }), UncertainOutcome);
    await new Promise((resolve) => setTimeout(resolve, 900));
    assert.deepEqual((await pilot.call(tools.write_count.id, {})).structured_content, { calls: 1 });
    await pilot.call(tools.write_count.id, {});
    await assert.rejects(pilot.call(tools.write_count.id, {}), BudgetExceeded);
  } finally {
    await pilot.close();
  }
});

test("Streamable HTTP against a real Python MCP server", async () => {
  const port = await freePort();
  const server = spawn(PYTHON, [RUNTIME_SERVER, "http", String(port)], { stdio: "ignore" });
  try {
    await waitForPort(port, server);
    const integration = parseIntegration({
      id: "test/http", service: "calculator", publisher: "test", version: "1", transport: "http",
      endpoint: `http://127.0.0.1:${port}/mcp`, support: "supported", capabilities: ["math.add"],
      tools: { add: { capability: "math.add" } },
    });
    const pilot = new MCPilot({ userId: "alice", catalog: new Catalog([integration]),
      policy: new Policy([integration], { capabilities: ["math.add"], allowLoopback: true }) });
    try {
      const toolset = await pilot.toolsFor({ task: "add", services: ["calculator"] });
      assert.deepEqual((await pilot.call(toolset.tools[0].id, { left: 8, right: 3 })).structured_content, { sum: 11 });
      const denied = new MCPilot({ userId: "alice", catalog: new Catalog([integration]),
        policy: new Policy([integration], { capabilities: ["math.add"] }) });
      await assert.rejects(denied.connect("test/http"), PolicyDenied); // loopback needs explicit approval
    } finally {
      await pilot.close();
    }
  } finally {
    server.kill();
  }
});

test("discovery meta-tools and a durable workflow over the same SDK", async () => {
  const pilot = await pilotFor(calculator());
  try {
    const tools = new DiscoveryTools(pilot);
    assert.equal((await tools.handle("shell", {})).error, "UnknownTool");
    assert.equal((await tools.handle(FIND_TOOLS, { task: "x", endpoint: "https://evil" })).error, "InvalidArguments");
    const found = await tools.handle(FIND_TOOLS, { task: "policz", services: ["calculator"] }) as { tools: { id: string; name: string }[]; needs_user: unknown[] };
    assert.deepEqual(found.tools.map((t) => t.name), ["add", "process_info"]);
    assert.ok(!JSON.stringify(found).includes(RUNTIME_SERVER));
    const add = found.tools.find((t) => t.name === "add")!;
    const called = await tools.handle(CALL_TOOL, { tool_id: add.id, arguments: { left: 4, right: 5 } });
    assert.deepEqual(called.structured_content, { sum: 9 });

    for (const store of [new MemoryWorkflowStore(), new JsonFileWorkflowStore(await tempDir())]) {
      const runner = new WorkflowRunner(pilot, store);
      const id = await runner.create([{ integration_id: "test/calculator", capability: "math.add", tool_name: "add",
                                        arguments: { left: 20, right: 22 } }], { task: "add" });
      const run = await runner.resume(id);
      assert.equal(run.status, "complete");
      assert.deepEqual(run.steps[0].result?.structured_content, { sum: 42 });
      await assert.rejects(store.load(id, "mallory"));
      const release = await store.lock(id);
      await assert.rejects(runner.resume(id), /already executing/);
      await release();
    }
  } finally {
    await pilot.close();
  }
});

test("installer validation rejects unpinned or injected package specs without I/O", () => {
  const base = { runtime: "node" as const, name: "safe", version: "1.0.0", entrypoint: "safe", args: [], dependencies: [] };
  assert.deepEqual(pinnedRequirements(base), ["safe@1.0.0"]);
  for (const bad of [{ version: "latest" }, { name: "https://evil/x" }, { entrypoint: "../evil" }, { dependencies: ["x@^2"] },
                     { runtime: "python" as const, version: ">=1" }, { runtime: "python" as const, entrypoint: null }]) {
    assert.throws(() => pinnedRequirements({ ...base, ...bad }), JSON.stringify(bad));
  }
});

test("budget is per task: a long gateway-style session keeps working; audit matches the Python shape", async () => {
  const { JsonlAuditSink } = await import("../src/node.js");
  const { readFile, stat } = await import("node:fs/promises");
  const { join } = await import("node:path");
  const { vectors } = await import("./helpers.js");
  const log = join(await tempDir(), "audit.jsonl");
  const sink = new JsonlAuditSink(log);
  const events: AuditEvent[] = [];
  const pilot = await pilotFor(calculator(), { tenant: "acme", audit: (e) => { events.push(e); return sink.write(e); } });
  try {
    const tools = new DiscoveryTools(pilot);
    const addId = async () => ((await tools.handle(FIND_TOOLS, { task: "policz", services: ["calculator"] })) as
      { tools: { id: string; name: string }[] }).tools.find((t) => t.name === "add")!.id;
    let id = await addId();
    const results = [];
    for (let i = 0; i < 21; i++) results.push(await tools.handle(CALL_TOOL, { tool_id: id, arguments: { left: i, right: 1 } }));
    assert.equal(results.filter((r) => !("error" in r)).length, 20);
    assert.equal(results[20].error, "BudgetExceeded");
    assert.match(String(results[20].message), /new task/);
    id = await addId();
    for (let i = 0; i < 5; i++) {
      assert.deepEqual((await tools.handle(CALL_TOOL, { tool_id: id, arguments: { left: i, right: 1 } })).structured_content, { sum: i + 1 });
    }
  } finally {
    await pilot.close();
  }
  const calls = events.filter((e) => e.action === "call");
  assert.equal(new Set(calls.map((e) => e.task_id)).size, 2);
  assert.ok(calls.every((e) => e.tenant === "acme" && typeof e.duration_ms === "number"));
  assert.deepEqual(Object.keys(events[0]).sort(), [...vectors.audit_event_fields].sort());
  const lines = (await readFile(log, "utf8")).trim().split("\n").map((l) => JSON.parse(l));
  assert.equal(lines.length, events.length);
  assert.equal((await stat(log)).mode & 0o777, 0o600);
});
