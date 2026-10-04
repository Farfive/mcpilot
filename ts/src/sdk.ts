/** Asynchronous host-owned MCP orchestration. No model or agent loop is bundled. */

import { AjvJsonSchemaValidator } from "@modelcontextprotocol/client/validators/ajv";

import { AuthManager } from "./auth.js";
import { canonicalJson, sha256Hex } from "./canonical.js";
import type { Catalog } from "./catalog.js";
import {
  BudgetExceeded, ConnectionUnavailable, InvalidArguments, MCPilotError, PolicyDenied, UncertainOutcome,
} from "./errors.js";
import {
  DEFAULT_LIMITS, parseTaskRequest, type AuditEvent, type CallResult, type Connection, type Integration, type Limits,
  type Plan, type TaskRequest, type Tool, type ToolSet,
} from "./models.js";
import type { Policy } from "./policy.js";
import { compareCodePoints, route, type Reranker } from "./router.js";
import { Runtime, type RawTool, type Session } from "./runtime.js";

export type AuditSink = (event: AuditEvent) => void | Promise<void>;
export type TaskInput = string | Partial<TaskRequest>;

const validator = new AjvJsonSchemaValidator();
const utf8Length = (text: string) => new TextEncoder().encode(text).length;

/** Schemas are data: reject remote references and oversized payloads before compiling. */
export function validateSchema(schema: unknown): void {
  if (typeof schema !== "object" || schema === null || Array.isArray(schema)) throw new TypeError("Tool schema must be an object");
  if (utf8Length(JSON.stringify(schema)) > 100_000) throw new TypeError("Tool schema exceeds limit");
  const walk = (value: unknown): void => {
    if (Array.isArray(value)) return value.forEach(walk);
    if (value && typeof value === "object") {
      for (const [key, child] of Object.entries(value)) {
        if (["$ref", "$dynamicRef", "$recursiveRef"].includes(key) && (typeof child !== "string" || !child.startsWith("#"))) {
          throw new TypeError("External schema references are disabled");
        }
        walk(child);
      }
    }
  };
  walk(schema);
  validator.getValidator(schema as never);
}

function matches(schema: Record<string, unknown>, value: unknown): boolean {
  return validator.getValidator(schema as never)(value).valid;
}

class Mutex {
  private tail: Promise<void> = Promise.resolve();

  async run<T>(work: () => Promise<T>): Promise<T> {
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise((resolve) => { release = resolve; });
    await previous;
    try {
      return await work();
    } finally {
      release();
    }
  }
}

interface TaskBudget {
  taskId: string;
  steps: number;
  cost: number;
}

const elapsed = (started: number) => Math.round((performance.now() - started) * 1000) / 1000;
const newTaskId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

export interface MCPilotOptions {
  /** Host-authenticated user; never chosen by the model. */
  userId: string;
  catalog: Catalog;
  policy: Policy;
  auth?: AuthManager;
  runtime?: Runtime;
  limits?: Partial<Limits>;
  reranker?: Reranker;
  tokenCounter?: (text: string) => number;
  audit?: AuditSink;
  /** Organization of the user, recorded in audit events (e.g. set by a multi-tenant gateway). */
  tenant?: string;
}

export class MCPilot {
  readonly userId: string;
  readonly catalog: Catalog;
  readonly policy: Policy;
  readonly auth: AuthManager;
  readonly runtime: Runtime;
  readonly limits: Limits;
  private readonly reranker?: Reranker;
  // UTF-8 bytes is a deliberately conservative token upper bound.
  private readonly tokenCounter: (text: string) => number;
  private readonly audit?: AuditSink;
  readonly tenant: string | null;
  private budgets = new Map<string, TaskBudget>();
  private currentTask: string | null = null;
  private readonly connections = new Map<string, Connection>();
  private readonly sessions = new Map<string, Session>();
  private readonly schemas = new Map<string, RawTool[]>();
  private issued = new Map<string, Tool>();
  private readonly locks = new Map<string, Mutex>();
  private closed = false;

  constructor(options: MCPilotOptions) {
    if (!options.userId) throw new TypeError("Host-authenticated userId is required");
    this.userId = options.userId;
    this.catalog = options.catalog;
    this.policy = options.policy;
    this.auth = options.auth ?? new AuthManager();
    this.runtime = options.runtime ?? new Runtime();
    this.limits = { ...DEFAULT_LIMITS, ...options.limits };
    this.reranker = options.reranker;
    this.tokenCounter = options.tokenCounter ?? utf8Length;
    this.audit = options.audit;
    this.tenant = options.tenant ?? null;
  }

  private integration(id: string): Integration {
    if (!this.catalog.has(id)) throw new PolicyDenied("Integration is not in the host catalog");
    return this.catalog.get(id);
  }

  private async emit(action: AuditEvent["action"], outcome: string, fields: Partial<AuditEvent> = {}): Promise<void> {
    if (!this.audit) return;
    try {
      await this.audit({ at: new Date().toISOString(), user_id: this.userId, action, outcome, integration_id: null,
                         account: null, connection_id: null, tool: null, effect: null, tenant: this.tenant,
                         task_id: this.currentTask, duration_ms: null, ...fields });
    } catch {
      // The action already happened; never turn a completed write into an error.
    }
  }

  /** Small policy-filtered summaries, never the full registry or tool schemas. */
  async discover(task: TaskInput, { limit = 5 }: { limit?: number } = {}) {
    const plan = await this.plan(task);
    return plan.selections.slice(0, Math.max(0, Math.min(limit, 20))).map((selection) => {
      const integration = this.integration(selection.integration_id);
      return { id: integration.id, service: integration.service, capability: selection.capability,
               account: selection.account, support: integration.support, auth: integration.auth.mode };
    });
  }

  async plan(task: TaskInput): Promise<Plan> {
    const connected = [...this.connections.values()].filter((c) => c.status === "ready")
      .map((c) => [c.integration_id, c.account] as [string, string]);
    return route(parseTaskRequest(task), this.catalog.all(), this.policy,
                 { connected, ...(this.reranker ? { reranker: this.reranker } : {}) });
  }

  async connect(integrationId: string, { account = "default", capabilities }: { account?: string; capabilities?: string[] } = {}): Promise<Connection> {
    const before = this.connections.get(canonicalJson([integrationId, account]));
    const started = performance.now();
    let connection: Connection;
    try {
      connection = await this.connectInner(integrationId, account, capabilities);
    } catch (error) {
      if (error instanceof MCPilotError) {
        await this.emit("connect", error.name, { integration_id: integrationId, account, duration_ms: elapsed(started) });
      }
      throw error;
    }
    if (connection !== before) {
      await this.emit("connect", connection.status, { integration_id: integrationId, account, connection_id: connection.id,
                                                      duration_ms: elapsed(started) });
    }
    return connection;
  }

  private async connectInner(integrationId: string, account: string, capabilities?: string[]): Promise<Connection> {
    if (this.closed) throw new ConnectionUnavailable("SDK is closed");
    const integration = this.integration(integrationId);
    const caps = [...new Set(capabilities ?? integration.capabilities.filter((c) => this.policy.capabilities.has(c)))]
      .sort(compareCodePoints);
    this.policy.check(integration, null, account);
    if (!caps.length) throw new PolicyDenied("No capabilities authorized for this connection");
    for (const cap of caps) this.policy.check(integration, cap, account);
    const key = canonicalJson([integrationId, account]);
    if (!this.locks.has(key)) this.locks.set(key, new Mutex());
    return this.locks.get(key)!.run(async () => {
      const previous = this.connections.get(key);
      if (previous?.status === "ready" && caps.every((c) => previous.capabilities.includes(c))) {
        try {
          await this.sessions.get(previous.id)!.ping();
          return previous;
        } catch {
          await this.drop(previous);
        }
      } else if (previous) {
        await this.drop(previous);
      }
      const handle = await this.auth.prepare(this.userId, integration, account, caps);
      let connection: Connection = { id: handle.connectionId, integration_id: integrationId, account,
                                     status: handle.status, capabilities: caps, message: handle.reason };
      this.connections.set(key, connection);
      if (handle.status !== "ready") return connection;
      let session: Session | undefined;
      try {
        session = await this.runtime.open(integration, handle);
        const names = new Set<string>();
        const valid: RawTool[] = [];
        for (const schema of await session.listTools()) {
          if (names.has(schema.name)) throw new TypeError("Duplicate MCP tool names");
          names.add(schema.name);
          const rule = integration.tools[schema.name];
          // Unmapped tools and effects outside policy are hidden, not fatal.
          if (!rule || !caps.includes(rule.capability) || !this.policy.effects.has(rule.effect)) continue;
          this.policy.checkTool(integration, rule, account);
          validateSchema(schema.input_schema);
          if (schema.output_schema) validateSchema(schema.output_schema);
          valid.push(schema);
        }
        this.sessions.set(connection.id, session);
        this.schemas.set(connection.id, valid);
        connection = { ...connection, status: valid.length ? "ready" : "connected", message: null };
      } catch (error) {
        if (session) await session.close();
        const status = (error as { status?: string })?.status;
        const blocked = status === "auth_required" || status === "setup_required";
        connection = { ...connection, status: blocked ? status : "error",
                       message: blocked ? "Connection needs authorization or setup"
                         : "Connection failed; check host configuration and provider availability" };
      }
      this.connections.set(key, connection);
      return connection;
    });
  }

  async toolsFor(task: TaskInput | Plan, { tokenBudget }: { tokenBudget?: number } = {}): Promise<ToolSet> {
    const plan = (task as Plan).selections !== undefined ? task as Plan : await this.plan(task as TaskInput);
    const groups = new Map<string, { id: string; account: string; capabilities: Set<string> }>();
    for (const s of plan.selections) {
      const key = canonicalJson([s.integration_id, s.account]);
      if (!groups.has(key)) groups.set(key, { id: s.integration_id, account: s.account, capabilities: new Set() });
      groups.get(key)!.capabilities.add(s.capability);
    }
    const budget = Math.min(tokenBudget ?? this.limits.tool_tokens, this.limits.tool_tokens);
    let used = this.tokenCounter("[]");
    let truncated = false;
    const tools: Tool[] = [];
    const connections: Connection[] = [];
    const budgetTask: TaskBudget = { taskId: newTaskId(), steps: 0, cost: 0 };
    const previousTask = this.currentTask;
    this.currentTask = budgetTask.taskId;
    try {
    for (const { id, account, capabilities } of groups.values()) {
      const integration = this.integration(id);
      const connection = await this.connect(id, { account, capabilities: [...capabilities].sort(compareCodePoints) });
      connections.push(connection);
      if (connection.status !== "ready") continue;
      for (const schema of [...this.schemas.get(connection.id)!].sort((a, b) => compareCodePoints(a.name, b.name))) {
        const rule = integration.tools[schema.name];
        if (!capabilities.has(rule.capability)) continue;
        this.policy.checkTool(integration, rule, account);
        const tool: Tool = {
          id: "mcp_" + sha256Hex(`${connection.id}:${schema.name}`).slice(0, 32), connection_id: connection.id,
          integration_id: id, name: schema.name, description: Array.from(schema.description).slice(0, 2000).join(""),
          input_schema: schema.input_schema, output_schema: schema.output_schema, capability: rule.capability,
          effect: rule.effect, untrusted: true,
        };
        const cost = this.tokenCounter(JSON.stringify(tool)) + 32;
        if (used + cost > budget) {
          truncated = true;
          continue;
        }
        used += cost;
        tools.push(tool);
        this.issued.set(tool.id, tool);
        this.budgets.set(tool.id, budgetTask); // the latest task that issued a tool pays for it
      }
    }
    } finally {
      this.currentTask = previousTask;
    }
    return { tools, connections, token_estimate: used, truncated, task_id: budgetTask.taskId };
  }

  async call(toolId: string, args: Record<string, unknown>,
             { idempotencyKey, signal }: { idempotencyKey?: string; signal?: AbortSignal } = {}): Promise<CallResult> {
    const tool = this.issued.get(toolId);
    const fields = tool ? { integration_id: tool.integration_id, connection_id: tool.connection_id, tool: tool.name,
                            effect: tool.effect, task_id: this.budgets.get(toolId)?.taskId ?? null } : {};
    const started = performance.now();
    let result: CallResult;
    try {
      result = await this.callInner(toolId, args, idempotencyKey, signal);
    } catch (error) {
      if (error instanceof MCPilotError) await this.emit("call", error.name, { ...fields, duration_ms: elapsed(started) });
      throw error;
    }
    await this.emit("call", result.is_error ? "tool_error" : "ok", { ...fields, duration_ms: elapsed(started) });
    return result;
  }

  private async callInner(toolId: string, args: Record<string, unknown>, idempotencyKey?: string,
                          signal?: AbortSignal): Promise<CallResult> {
    const tool = this.issued.get(toolId);
    if (!tool) throw new PolicyDenied("Tool was not exposed by toolsFor in this user session");
    const integration = this.integration(tool.integration_id);
    const connection = [...this.connections.values()].find((c) => c.id === tool.connection_id);
    if (!connection || connection.status !== "ready") throw new ConnectionUnavailable("Tool connection is not ready");
    const rule = integration.tools[tool.name];
    this.policy.checkTool(integration, rule, connection.account);
    const payload = { ...args };
    if (idempotencyKey) {
      if (!rule.idempotency_parameter) throw new PolicyDenied("Provider adapter does not support idempotency keys");
      payload[rule.idempotency_parameter] = idempotencyKey;
    }
    if (!matches(tool.input_schema, payload)) throw new InvalidArguments("Arguments do not match the discovered MCP tool schema");
    const task = this.budgets.get(toolId)!;
    const mutation = rule.effect === "write" || rule.effect === "send";
    const attempts = 1 + (rule.effect === "read" ? this.limits.read_retries : 0);
    for (let attempt = 0; attempt < attempts; attempt++) {
      if (task.steps >= this.limits.max_steps || task.cost + integration.cost_per_call > this.limits.max_cost) {
        throw new BudgetExceeded("Step or estimated cost limit reached for this task; request tools again to start a new task");
      }
      task.steps += 1;
      task.cost += integration.cost_per_call;
      let raw;
      try {
        raw = await this.sessions.get(connection.id)!.callTool(tool.name, payload, signal);
      } catch {
        if (mutation) throw new UncertainOutcome("Mutation outcome is unknown; reconcile before retrying");
        if (signal?.aborted) throw new ConnectionUnavailable("Tool call was cancelled");
        if (attempt + 1 < attempts) {
          await new Promise((resolve) => setTimeout(resolve, 50 * (attempt + 1)));
          continue;
        }
        throw new ConnectionUnavailable("Tool call failed after bounded read retries");
      }
      if (utf8Length(JSON.stringify(raw)) > this.limits.max_result_bytes) {
        throw new BudgetExceeded("Tool result exceeds context limit; narrow the query");
      }
      if (tool.output_schema && raw.structured_content !== null && !raw.is_error
          && !matches(tool.output_schema, raw.structured_content)) {
        throw new ConnectionUnavailable("Tool returned an invalid output schema");
      }
      return { tool_id: tool.id, content: raw.content, structured_content: raw.structured_content,
               is_error: raw.is_error, untrusted: true };
    }
    throw new Error("unreachable");
  }

  async status(integrationId?: string): Promise<Connection[]> {
    return [...this.connections.values()].filter((c) => integrationId === undefined || c.integration_id === integrationId);
  }

  private async drop(connection: Connection): Promise<void> {
    const session = this.sessions.get(connection.id);
    this.sessions.delete(connection.id);
    if (session) {
      await session.close();
      this.runtime.forget(session);
    }
    this.schemas.delete(connection.id);
    this.issued = new Map([...this.issued].filter(([, tool]) => tool.connection_id !== connection.id));
    this.budgets = new Map([...this.budgets].filter(([id]) => this.issued.has(id)));
  }

  async disconnect(integrationId: string, { account = "default", revoke = false }: { account?: string; revoke?: boolean } = {}): Promise<void> {
    const key = canonicalJson([integrationId, account]);
    const connection = this.connections.get(key);
    if (connection && connection.status !== "disconnected") {
      await this.drop(connection);
      this.connections.set(key, { ...connection, status: "disconnected" });
      await this.emit("disconnect", "ok", { integration_id: integrationId, account, connection_id: connection.id });
    }
    if (revoke) {
      const integration = this.integration(integrationId);
      try {
        await this.auth.revoke(this.userId, integrationId, account, integration.endpoint);
      } catch (error) {
        await this.emit("revoke", "failed", { integration_id: integrationId, account });
        throw error;
      }
      await this.emit("revoke", "ok", { integration_id: integrationId, account });
    }
  }

  async close(): Promise<void> {
    if (this.closed) return;
    for (const connection of [...this.connections.values()]) {
      await this.disconnect(connection.integration_id, { account: connection.account });
    }
    await this.runtime.close();
    this.closed = true;
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }
}
