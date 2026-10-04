/** OAuth through the official TypeScript SDK against the Python fixture IdP, and URL elicitation against the Python remote gateway. */

import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";

import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";

import {
  AuthManager, Catalog, DiscoveryTools, FIND_TOOLS, LoginBroker, MCPilot, parseIntegration, Policy,
  type AuthorizationRequest,
} from "../src/index.js";
import { EncryptedFileSecretStore, Fernet } from "../src/node.js";
import { pythonFixture, tempDir } from "./helpers.js";

/** Simulates the user's consent click at the fixture IdP; returns the redirect to the host. */
async function consent(url: string): Promise<string> {
  const response = await fetch(url + "&fixture_consent=allow", { redirect: "manual" });
  assert.equal(response.status, 302);
  return response.headers.get("location")!;
}

test("login button -> provider consent -> host callback -> ready; tokens reused after restart", async () => {
  const fixture = await pythonFixture("oauth");
  try {
    const integration = parseIntegration({
      id: "fixture/docs", service: "docs", publisher: "fixture", version: "1", transport: "http",
      endpoint: fixture.info.endpoint, support: "supported", capabilities: ["documents.search"],
      tools: { documents_search: { capability: "documents.search" } },
      auth: { mode: "oauth", scopes_by_capability: { "documents.search": ["documents.read"] } },
    });
    const catalog = new Catalog([integration]);
    const policy = new Policy([integration], { capabilities: ["documents.search"], allowLoopback: true });
    const directory = await tempDir();
    const key = Fernet.generateKey();
    const buttons: AuthorizationRequest[] = [];
    const broker = new LoginBroker((request) => { buttons.push(request); });
    const auth = new AuthManager(new EncryptedFileSecretStore(directory, key), { login: broker });
    const pilot = new MCPilot({ userId: "alice", catalog, policy, auth });
    try {
      const waiting = await pilot.connect("fixture/docs");
      assert.equal(waiting.status, "auth_required");
      assert.equal(buttons.length, 1);
      const button = buttons[0];
      assert.equal(button.connectionId, waiting.id);
      const url = new URL(button.url);
      assert.equal(url.searchParams.get("code_challenge_method"), "S256");
      // Minimal policy scope; offline_access is the only implicit addition (never documents.write).
      assert.deepEqual(url.searchParams.get("scope")!.split(" ").sort(), ["documents.read", "offline_access"]);
      assert.ok(!JSON.stringify(waiting).includes("authorize"));

      const discovery = new DiscoveryTools(pilot, { login: broker });
      const pending = await discovery.handle(FIND_TOOLS, { task: "szukaj", services: ["docs"] });
      assert.deepEqual(pending.needs_user, [{ integration_id: "fixture/docs", account: "default", action: "finish_login" }]);
      assert.ok(!JSON.stringify(pending).includes(button.url));

      const callback = await consent(buttons.at(-1)!.url);
      assert.equal(await broker.complete({ userId: "mallory", callbackUrl: callback }), false);
      assert.equal(await broker.complete({ userId: "alice", callbackUrl: callback }), true);
      assert.equal(await broker.complete({ userId: "alice", callbackUrl: callback }), false); // single use

      const toolset = await pilot.toolsFor({ task: "szukaj", services: ["docs"] });
      assert.equal(toolset.connections[0].status, "ready");
      const result = await pilot.call(toolset.tools[0].id, { query: "atlas" });
      assert.equal(result.content[0].text, "OAuth protected fixture document");
    } finally {
      await pilot.close();
    }
    for (const file of await readdir(directory)) {
      assert.ok(!(await readFile(join(directory, file), "utf8")).includes("fixture-access-token"));
    }

    // New process: same encrypted store, no login UI configured, no new button.
    const restarted = new MCPilot({ userId: "alice", catalog, policy,
      auth: new AuthManager(new EncryptedFileSecretStore(directory, key)) });
    try {
      assert.equal((await restarted.connect("fixture/docs")).status, "ready");
      assert.equal(buttons.length, 2);
    } finally {
      await restarted.close();
    }
  } finally {
    await fixture.stop();
  }
});

test("official TypeScript client completes URL elicitation against the Python remote gateway", async () => {
  const fixture = await pythonFixture("gateway");
  const gateway = fixture.info.gateway;
  const client = new Client({ name: "ts-host", version: "1.0.0" },
    { capabilities: { elicitation: { url: {} } }, versionNegotiation: { mode: "auto" } });
  const prompts: { mode?: string; url?: string }[] = [];
  const browsers: Promise<void>[] = [];
  client.setRequestHandler("elicitation/create", async (request) => {
    const params = request.params as { mode?: string; url?: string };
    prompts.push(params);
    browsers.push((async () => {
      // The user's browser: gateway page (identity check, cookie) -> provider consent -> gateway callback.
      const page = await fetch(params.url!, { headers: { "X-Test-User": "alice" }, redirect: "manual" });
      assert.equal(page.status, 302);
      const cookie = page.headers.get("set-cookie")!.split(";")[0];
      const callback = await consent(page.headers.get("location")!);
      const done = await fetch(callback, { headers: { cookie }, redirect: "manual" });
      assert.equal(done.status, 200);
    })());
    return { action: "accept" };
  });
  try {
    await client.connect(new StreamableHTTPClientTransport(new URL(`${gateway}/mcp`),
      { requestInit: { headers: { Authorization: "Bearer token-alice" } } }));
    assert.equal(client.getNegotiatedProtocolVersion(), "2026-07-28");
    const found = await client.callTool({ name: FIND_TOOLS, arguments: { task: "Szukaj dokumentów", services: ["docs"] } });
    await Promise.all(browsers);
    const payload = found.structuredContent as { tools: { id: string; name: string }[]; needs_user: unknown[] };
    assert.equal(prompts.length, 1);
    assert.equal(prompts[0].mode, "url");
    assert.ok(prompts[0].url!.startsWith(`${gateway}/connect/`));
    assert.deepEqual(payload.tools.map((t) => t.name), ["documents_search"]);
    assert.deepEqual(payload.needs_user, []);
    const result = await client.callTool({ name: "mcpilot_call_tool",
      arguments: { tool_id: payload.tools[0].id, arguments: { query: "x" } } });
    assert.equal((result.structuredContent as { content: { text: string }[] }).content[0].text, "OAuth protected fixture document");
  } finally {
    await client.close();
    await fixture.stop();
  }
});
