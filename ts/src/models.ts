/**
 * Public, secret-free contracts. Data fields use snake_case so the same manifest
 * and workflow JSON works in the Python and TypeScript SDKs.
 */

export type Effect = "read" | "draft" | "write" | "send";
export type Support = "discovered" | "supported" | "experimental" | "unavailable";
export type AuthMode = "none" | "oauth" | "bearer" | "api_key";
export type ConnectionStatus =
  | "discovered" | "supported" | "approved" | "auth_required" | "setup_required"
  | "connected" | "ready" | "disconnected" | "error";

export interface PackageSpec {
  runtime: "python" | "node";
  name: string;
  version: string;
  entrypoint: string | null;
  args: string[];
  dependencies: string[];
}

export interface AuthSpec {
  mode: AuthMode;
  scopes_by_capability: Record<string, string[]>;
  setup_required: string | null;
  header_name: string;
  env_var: string | null;
  target_service_auth: string;
}

/** Trusted host mapping; server annotations cannot grant permissions. */
export interface ToolRule {
  capability: string;
  effect: Effect;
  idempotency_parameter: string | null;
}

export interface Integration {
  id: string;
  service: string;
  publisher: string;
  version: string;
  transport: "http" | "stdio";
  endpoint: string | null;
  command: string[];
  package: PackageSpec | null;
  capabilities: string[];
  tools: Record<string, ToolRule>;
  auth: AuthSpec;
  support: Support;
  source: string;
  quality: number;
  cost_per_call: number;
  latency_ms: number;
}

export interface TaskRequest {
  task: string;
  capabilities: string[];
  services: string[];
  accounts: Record<string, string>;
}

export interface Requirement {
  capability: string;
  service: string | null;
  account: string;
}

export interface Selection {
  integration_id: string;
  capability: string;
  account: string;
  score: number;
  reason: string;
}

export interface Unavailable {
  integration_id: string;
  capability: string;
  reason: "setup_required" | "unsupported" | "policy_denied" | "no_tool_mapping";
}

export interface Plan {
  task: string;
  selections: Selection[];
  missing: Requirement[];
  unavailable: Unavailable[];
}

export interface Connection {
  id: string;
  integration_id: string;
  account: string;
  status: ConnectionStatus;
  capabilities: string[];
  message: string | null;
}

export interface Tool {
  id: string;
  connection_id: string;
  integration_id: string;
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown> | null;
  capability: string;
  effect: Effect;
  untrusted: true;
}

export interface ToolSet {
  tools: Tool[];
  connections: Connection[];
  token_estimate: number;
  truncated: boolean;
  /** Calls on these tools share one step/cost budget; a new toolsFor starts a new task. */
  task_id: string | null;
}

export interface CallResult {
  tool_id: string;
  content: Record<string, unknown>[];
  structured_content: unknown;
  is_error: boolean;
  untrusted: true;
}

/** `max_steps`/`max_cost` bound one task: a toolsFor call and the calls on its tools. */
export interface Limits {
  max_steps: number;
  max_cost: number;
  tool_tokens: number;
  max_result_bytes: number;
  read_retries: number;
}

/** Secret-free record: no arguments, results, URLs or credentials. */
export interface AuditEvent {
  at: string;
  user_id: string;
  action: "connect" | "call" | "disconnect" | "revoke";
  outcome: string;
  integration_id: string | null;
  account: string | null;
  connection_id: string | null;
  tool: string | null;
  effect: string | null;
  tenant: string | null;
  task_id: string | null;
  duration_ms: number | null;
}

export const DEFAULT_LIMITS: Readonly<Limits> = Object.freeze({
  max_steps: 20, max_cost: 1, tool_tokens: 4000, max_result_bytes: 100_000, read_retries: 1,
});

export const DEFAULT_TARGET_SERVICE_AUTH = "Managed by the MCP server; independent of MCP client authorization.";

const EFFECTS = new Set(["read", "draft", "write", "send"]);
const SUPPORT = new Set(["discovered", "supported", "experimental", "unavailable"]);
const AUTH_MODES = new Set(["none", "oauth", "bearer", "api_key"]);

class Reader {
  constructor(private readonly value: Record<string, unknown>, private readonly path: string) {}

  static object(value: unknown, path: string, allowed: string[]): Reader {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new TypeError(`${path} must be an object`);
    }
    const extra = Object.keys(value).filter((key) => !allowed.includes(key));
    if (extra.length) throw new TypeError(`${path} has unknown fields: ${extra.join(", ")}`);
    return new Reader(value as Record<string, unknown>, path);
  }

  has(key: string): boolean {
    return this.value[key] !== undefined;
  }

  raw(key: string): unknown {
    return this.value[key];
  }

  string(key: string, fallback?: string | null): string {
    const value = this.value[key] ?? fallback;
    if (typeof value !== "string") throw new TypeError(`${this.path}.${key} must be a string`);
    return value;
  }

  optionalString(key: string): string | null {
    const value = this.value[key];
    if (value === undefined || value === null) return null;
    if (typeof value !== "string") throw new TypeError(`${this.path}.${key} must be a string or null`);
    return value;
  }

  choice<T extends string>(key: string, options: Set<string>, fallback?: T): T {
    const value = this.string(key, fallback);
    if (!options.has(value)) throw new TypeError(`${this.path}.${key} has an invalid value`);
    return value as T;
  }

  strings(key: string): string[] {
    const value = this.value[key] ?? [];
    if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
      throw new TypeError(`${this.path}.${key} must be a list of strings`);
    }
    return [...value];
  }

  number(key: string, fallback: number, min: number, max = Infinity): number {
    const value = this.value[key] ?? fallback;
    if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) {
      throw new TypeError(`${this.path}.${key} must be a finite number in range`);
    }
    return value;
  }

  record<T>(key: string, parse: (value: unknown, path: string) => T): Record<string, T> {
    const value = this.value[key] ?? {};
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new TypeError(`${this.path}.${key} must be an object`);
    }
    return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, parse(item, `${this.path}.${key}.${name}`)]));
  }
}

function parsePackage(value: unknown, path: string): PackageSpec {
  const r = Reader.object(value, path, ["runtime", "name", "version", "entrypoint", "args", "dependencies"]);
  return {
    runtime: r.choice("runtime", new Set(["python", "node"])),
    name: r.string("name"),
    version: r.string("version"),
    entrypoint: r.optionalString("entrypoint"),
    args: r.strings("args"),
    dependencies: r.strings("dependencies"),
  };
}

function parseAuth(value: unknown, path: string): AuthSpec {
  const r = Reader.object(value ?? {}, path,
    ["mode", "scopes_by_capability", "setup_required", "header_name", "env_var", "target_service_auth"]);
  return {
    mode: r.choice("mode", AUTH_MODES, "none"),
    scopes_by_capability: r.record("scopes_by_capability", (item, itemPath) => {
      if (!Array.isArray(item) || !item.every((s) => typeof s === "string")) {
        throw new TypeError(`${itemPath} must be a list of strings`);
      }
      return [...item] as string[];
    }),
    setup_required: r.optionalString("setup_required"),
    header_name: r.string("header_name", "Authorization"),
    env_var: r.optionalString("env_var"),
    target_service_auth: r.string("target_service_auth", DEFAULT_TARGET_SERVICE_AUTH),
  };
}

function parseToolRule(value: unknown, path: string): ToolRule {
  const r = Reader.object(value, path, ["capability", "effect", "idempotency_parameter"]);
  return {
    capability: r.string("capability"),
    effect: r.choice("effect", EFFECTS, "read"),
    idempotency_parameter: r.optionalString("idempotency_parameter"),
  };
}

/** Validate a host manifest and fill defaults exactly like the Python `Integration` model. */
export function parseIntegration(value: unknown): Integration {
  const r = Reader.object(value, "integration", [
    "id", "service", "publisher", "version", "transport", "endpoint", "command", "package", "capabilities",
    "tools", "auth", "support", "source", "quality", "cost_per_call", "latency_ms",
  ]);
  const integration: Integration = {
    id: r.string("id"),
    service: r.string("service"),
    publisher: r.string("publisher"),
    version: r.string("version"),
    transport: r.choice("transport", new Set(["http", "stdio"])),
    endpoint: r.optionalString("endpoint"),
    command: r.strings("command"),
    package: r.has("package") && r.raw("package") !== null ? parsePackage(r.raw("package"), "integration.package") : null,
    capabilities: r.strings("capabilities"),
    tools: r.record("tools", parseToolRule),
    auth: parseAuth(r.raw("auth"), "integration.auth"),
    support: r.choice("support", SUPPORT, "discovered"),
    source: r.string("source", "host"),
    quality: r.number("quality", 0.5, 0, 1),
    cost_per_call: r.number("cost_per_call", 0, 0),
    latency_ms: r.number("latency_ms", 100, 0),
  };
  if (integration.transport === "http" && (!integration.endpoint || integration.command.length || integration.package)) {
    throw new TypeError("HTTP integrations need an endpoint and no local command/package");
  }
  if (integration.transport === "stdio" && integration.endpoint) {
    throw new TypeError("stdio integrations cannot have an endpoint");
  }
  if (integration.transport === "stdio" && integration.command.length && integration.package) {
    throw new TypeError("Choose either a command or a package");
  }
  return integration;
}

export function parseTaskRequest(value: string | Partial<TaskRequest>): TaskRequest {
  const input = typeof value === "string" ? { task: value } : value;
  const r = Reader.object(input, "task", ["task", "capabilities", "services", "accounts"]);
  return {
    task: r.string("task"),
    capabilities: r.strings("capabilities"),
    services: r.strings("services"),
    accounts: r.record("accounts", (item, path) => {
      if (typeof item !== "string") throw new TypeError(`${path} must be a string`);
      return item;
    }),
  };
}

export function deepFreeze<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}

export function clone<T>(value: T): T {
  return structuredClone(value);
}
