/** Cross-language contract: spec/vectors.json is generated from the Python implementation. */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  canonicalJson, connectionIdFor, credentialKey, DEFINITIONS, fingerprint, parseIntegration, parseTaskRequest,
  parseWorkflowRun, Policy, PolicyDenied, requirements, route, sha256Hex, summarize,
} from "../src/index.js";
import { Fernet } from "../src/node.js";
import { vectors } from "./helpers.js";

test("RFC 8785 canonical JSON matches Python byte for byte", () => {
  for (const { value, json } of vectors.canonical) assert.equal(canonicalJson(value), json, JSON.stringify(value));
  assert.equal(sha256Hex(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert.equal(sha256Hex("a".repeat(1000)), "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3");
});

test("manifests normalize to the Python model and share fingerprints", () => {
  for (const { input, normalized, fingerprint: expected } of vectors.integrations) {
    const parsed = parseIntegration(input);
    assert.deepEqual(parsed, normalized);
    assert.equal(fingerprint(parsed), expected, input.id);
  }
  for (const invalid of vectors.invalid_integrations) assert.throws(() => parseIntegration(invalid), JSON.stringify(invalid));
});

test("endpoint policy matches Python, including IP special ranges and parser edge cases", () => {
  for (const { endpoint, allow_loopback, allowed } of vectors.endpoints) {
    const policy = new Policy([], { allowLoopback: allow_loopback });
    const actual = (() => {
      try {
        policy.checkEndpoint(endpoint);
        return true;
      } catch (error) {
        assert.ok(error instanceof PolicyDenied);
        return false;
      }
    })();
    assert.equal(actual, allowed, `${endpoint} (loopback=${allow_loopback})`);
  }
});

test("credential keys, connection ids and tool ids are shared with Python", () => {
  for (const v of vectors.credentials) {
    const key = credentialKey(v.user_id, v.integration_id, v.account, v.endpoint);
    assert.equal(key.digest, v.digest);
    assert.equal(connectionIdFor(key), v.connection_id);
    for (const [name, id] of Object.entries(v.tool_ids)) {
      assert.equal("mcp_" + sha256Hex(`${v.connection_id}:${name}`).slice(0, 32), id);
    }
  }
});

test("task requirements match Python (Polish and English, Unicode word boundaries)", () => {
  for (const { request, expected } of vectors.requirements) {
    assert.deepEqual(requirements(parseTaskRequest(request)), expected, request.task);
  }
});

test("routing plans match Python: candidates, scores, reuse, reasons", async () => {
  for (const v of vectors.routes) {
    const policy = new Policy(v.approved.map(parseIntegration), {
      capabilities: v.policy.capabilities, effects: v.policy.effects, accounts: v.policy.accounts,
      allowLoopback: v.policy.allow_loopback, allowLocal: v.policy.allow_local,
    });
    const plan = await route(parseTaskRequest(v.request), v.catalog.map(parseIntegration), policy, { connected: v.connected });
    assert.deepEqual(plan, v.plan, v.name);
  }
});

test("registry summaries strip launch data and normalize text like Python", () => {
  for (const v of vectors.registry) {
    if (v.error) assert.throws(() => summarize(v.entry));
    else assert.deepEqual(summarize(v.entry), v.summary);
  }
});

test("discovery meta-tools and workflow JSON are identical", () => {
  assert.deepEqual(structuredClone(DEFINITIONS), vectors.discovery_definitions);
  const run = parseWorkflowRun(vectors.workflow_run);
  assert.equal(run.steps[0].result?.content[0].text, "#42");
  assert.equal(run.status, "waiting_for_auth");
});

test("Fernet credentials written by Python decrypt in TypeScript and vice versa", () => {
  const { key, plaintext, time, iv, token } = vectors.fernet;
  const fernet = new Fernet(key);
  assert.equal(fernet.decrypt(token).toString("utf8"), plaintext);
  assert.equal(fernet.encrypt(Buffer.from(plaintext), time * 1000, Uint8Array.from(iv)), token);
  assert.throws(() => new Fernet(Fernet.generateKey()).decrypt(token));
});
