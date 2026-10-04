/**
 * On-demand MCP sessions. HTTP works in any runtime with fetch; stdio and package
 * installation load Node-only modules lazily, so browser bundles never import them.
 */

import { Client, SdkError, SdkErrorCode, StreamableHTTPClientTransport, UnauthorizedError } from "@modelcontextprotocol/client";

import type { AuthHandle } from "./auth.js";
import { AuthFailure, AuthRequired, RuntimeFailure, RuntimeTimeout } from "./errors.js";
import type { Integration, PackageSpec } from "./models.js";

export interface RawTool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown> | null;
}

export interface RawCallResult {
  content: Record<string, unknown>[];
  structured_content: unknown;
  is_error: boolean;
}

export const VERSION = "0.1.0";

function sanitized(error: unknown, connecting = false): Error {
  if (error instanceof AuthFailure || error instanceof RuntimeFailure) return error;
  if (error instanceof UnauthorizedError) return new AuthRequired("Connection requires host login");
  const timeout = (error instanceof SdkError && error.code === SdkErrorCode.RequestTimeout)
    || (error as { name?: string })?.name === "TimeoutError" || (error as { code?: number })?.code === -32001;
  if (timeout) {
    return new RuntimeTimeout(connecting ? "MCP connection timed out" : "MCP operation timed out; execution outcome may be unknown");
  }
  return new RuntimeFailure(connecting ? "MCP connection failed or closed" : "MCP operation failed");
}

export class Session {
  private closed = false;

  constructor(private readonly client: Client, private readonly timeoutMs: number, private readonly maxTools: number) {}

  get isClosed(): boolean {
    return this.closed;
  }

  private options(signal?: AbortSignal) {
    return { timeout: this.timeoutMs, maxTotalTimeout: this.timeoutMs, ...(signal ? { signal } : {}) };
  }

  async listTools(signal?: AbortSignal): Promise<RawTool[]> {
    if (this.closed) throw new RuntimeFailure("MCP session is closed");
    const tools: RawTool[] = [];
    const names = new Set<string>();
    const seen = new Set<string>();
    let cursor: string | undefined;
    try {
      for (let page = 0; page < 100; page++) {
        const result = await this.client.listTools(cursor ? { cursor } : undefined,
          { ...this.options(signal), cacheMode: "bypass" });
        for (const tool of result.tools) {
          if (names.has(tool.name)) throw new RuntimeFailure("MCP server returned duplicate tool names");
          names.add(tool.name);
          tools.push({ name: tool.name, description: tool.description ?? "",
                       input_schema: tool.inputSchema as Record<string, unknown>,
                       output_schema: (tool.outputSchema as Record<string, unknown> | undefined) ?? null });
          if (tools.length > this.maxTools) throw new RuntimeFailure("MCP tool listing exceeds limit");
        }
        cursor = result.nextCursor;
        if (!cursor) return tools;
        if (seen.has(cursor)) throw new RuntimeFailure("MCP pagination cursor repeated");
        seen.add(cursor);
      }
    } catch (error) {
      throw sanitized(error);
    }
    throw new RuntimeFailure("MCP pagination exceeds limit");
  }

  async callTool(name: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<RawCallResult> {
    if (this.closed) throw new RuntimeFailure("MCP session is closed");
    try {
      const result = await this.client.callTool({ name, arguments: args }, this.options(signal));
      return { content: (result.content ?? []) as Record<string, unknown>[],
               structured_content: result.structuredContent ?? null, is_error: Boolean(result.isError) };
    } catch (error) {
      throw sanitized(error);
    }
  }

  /** Liveness without executing a tool (protocol 2026-07-28 removed ping). */
  async ping(): Promise<void> {
    await this.listTools();
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    try {
      await this.client.close();
    } catch {
      // Closing is best effort; the transport kills the child process.
    }
  }
}

/** Inherit runtime essentials only; credentials must be passed explicitly. */
export function sanitizedEnvironment(home: string, supplied: Record<string, string> = {}): Record<string, string> {
  const env: Record<string, string> = {
    PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: home, USERPROFILE: home, LOGNAME: "mcpilot", USER: "mcpilot",
    SHELL: "/bin/sh", TERM: "dumb", LANG: "C.UTF-8", PYTHONNOUSERSITE: "1",
    XDG_CONFIG_HOME: `${home}/.config`, XDG_CACHE_HOME: `${home}/.cache`, XDG_DATA_HOME: `${home}/.local/share`,
    npm_config_userconfig: process.platform === "win32" ? "NUL" : "/dev/null", npm_config_cache: `${home}/.npm`,
  };
  for (const key of ["SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"]) {
    if (process.env[key]) env[key] = process.env[key]!;
  }
  for (const [key, value] of Object.entries(supplied)) {
    if (key.includes("=") || (key + value).includes("\0")) throw new RuntimeFailure("Invalid subprocess environment");
    env[key] = value;
  }
  return env;
}

const PYTHON_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const PYTHON_VERSION = /^(?:\d+!)?\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?$/;
const NODE_NAME = /^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*$/;
const NODE_VERSION = /^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const ENTRYPOINT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

/** Same validation as the Python installer: exact versions, plain entrypoints, no injection. */
export function pinnedRequirements(spec: PackageSpec): string[] {
  const pin = (name: string, version: string) => {
    if (spec.runtime === "python") {
      if (!PYTHON_NAME.test(name) || !PYTHON_VERSION.test(version)) throw new RuntimeFailure("Python package must have an exact public version");
      return `${name}==${version}`;
    }
    if (!NODE_NAME.test(name) || !NODE_VERSION.test(version)) throw new RuntimeFailure("Node package must have an exact semantic version");
    return `${name}@${version}`;
  };
  const result = [pin(spec.name, spec.version)];
  for (const dependency of spec.dependencies) {
    if (spec.runtime === "python") {
      const parts = dependency.split("==");
      if (parts.length !== 2) throw new RuntimeFailure("Every Python dependency must use an exact == version");
      result.push(pin(parts[0], parts[1]));
    } else {
      const at = dependency.lastIndexOf("@");
      if (at <= 0) throw new RuntimeFailure("Every Node dependency must use an exact @version");
      result.push(pin(dependency.slice(0, at), dependency.slice(at + 1)));
    }
  }
  if (spec.entrypoint !== null && !ENTRYPOINT.test(spec.entrypoint)) throw new RuntimeFailure("Entrypoint must be a plain executable name");
  if (spec.runtime === "python" && !spec.entrypoint
      && !(spec.args.length >= 2 && spec.args[0] === "-m" && /^[A-Za-z_][A-Za-z0-9_.]*$/.test(spec.args[1]))) {
    throw new RuntimeFailure("Python packages need an explicit entrypoint or -m module arguments");
  }
  if (spec.runtime === "node" && !spec.entrypoint) throw new RuntimeFailure("Node packages need an explicit executable entrypoint");
  if (spec.args.some((arg) => arg.includes("\0"))) throw new RuntimeFailure("Package arguments contain an invalid character");
  return result;
}

export interface RuntimeOptions {
  /** Per-process working directories and package environments (Node only). */
  directory?: string;
  timeoutMs?: number;
  connectTimeoutMs?: number;
  maxTools?: number;
}

export class Runtime {
  readonly timeoutMs: number;
  readonly connectTimeoutMs: number;
  readonly maxTools: number;
  private readonly sessions = new Set<Session>();
  private installs = new Map<string, Promise<string[]>>();
  private closed = false;

  constructor(private readonly options: RuntimeOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.connectTimeoutMs = options.connectTimeoutMs ?? 30_000;
    this.maxTools = options.maxTools ?? 500;
  }

  async open(integration: Integration, handle: Pick<AuthHandle, "headers" | "env" | "authProvider" | "fetch">): Promise<Session> {
    if (this.closed) throw new RuntimeFailure("MCP runtime is closed");
    const client = new Client({ name: "MCPilot", version: VERSION },
      { capabilities: {}, versionNegotiation: { mode: "auto" } });
    let transport;
    if (integration.transport === "http") {
      if (!integration.endpoint) throw new RuntimeFailure("Remote MCP integration has no endpoint");
      transport = new StreamableHTTPClientTransport(new URL(integration.endpoint), {
        ...(handle.authProvider ? { authProvider: handle.authProvider } : {}),
        ...(handle.fetch ? { fetch: handle.fetch } : {}),
        requestInit: { headers: { ...handle.headers } },
      });
    } else {
      const command = integration.package ? await this.install(integration.package) : [...integration.command];
      if (!command.length || !command[0]) throw new RuntimeFailure("Local MCP integration has no launch command");
      const [{ StdioClientTransport }, fs, path, crypto] = await Promise.all([
        import("@modelcontextprotocol/client/stdio"), import("node:fs/promises"), import("node:path"), import("node:crypto"),
      ]);
      const directory = path.join(this.baseDirectory(), "processes", crypto.randomBytes(16).toString("hex"));
      const home = path.join(directory, "home");
      await fs.mkdir(home, { recursive: true, mode: 0o700 });
      transport = new StdioClientTransport({
        command: command[0], args: command.slice(1), env: sanitizedEnvironment(home, handle.env), cwd: directory, stderr: "ignore",
      });
    }
    try {
      await client.connect(transport, { timeout: this.connectTimeoutMs, maxTotalTimeout: this.connectTimeoutMs });
    } catch (error) {
      try { await client.close(); } catch { /* already failed */ }
      throw sanitized(error, true);
    }
    const session = new Session(client, this.timeoutMs, this.maxTools);
    this.sessions.add(session);
    return session;
  }

  private baseDirectory(): string {
    if (!this.options.directory) throw new RuntimeFailure("Local MCP integrations require Runtime({ directory })");
    return this.options.directory;
  }

  /** Explicit, pinned installation into a per-package directory (dependency isolation, not a sandbox). */
  private install(spec: PackageSpec): Promise<string[]> {
    const requirements = pinnedRequirements(spec);
    const key = JSON.stringify([spec, process.execPath, process.version]);
    let pending = this.installs.get(key);
    if (!pending) {
      pending = this.installPackage(spec, requirements, key).catch((error) => {
        this.installs.delete(key);
        throw error;
      });
      this.installs.set(key, pending);
    }
    return pending;
  }

  private async installPackage(spec: PackageSpec, requirements: string[], key: string): Promise<string[]> {
    const [fs, path, crypto, childProcess] = await Promise.all([
      import("node:fs/promises"), import("node:path"), import("node:crypto"), import("node:child_process"),
    ]);
    const destination = path.join(this.baseDirectory(), "packages", crypto.createHash("sha256").update(key).digest("hex").slice(0, 32));
    const bin = spec.runtime === "python" ? path.join(destination, process.platform === "win32" ? "Scripts" : "bin") : path.join(destination, "node_modules", ".bin");
    const command = spec.runtime === "python"
      ? [path.join(bin, spec.entrypoint ?? "python"), ...spec.args] : [path.join(bin, spec.entrypoint!), ...spec.args];
    const marker = path.join(destination, ".mcpilot-installed.json");
    const exists = async (file: string) => fs.access(file).then(() => true, () => false);
    if (await exists(marker) && await exists(command[0])) return command;
    await fs.rm(destination, { recursive: true, force: true });
    await fs.mkdir(destination, { recursive: true, mode: 0o700 });
    const run = (file: string, args: string[]) => new Promise<void>((resolve, reject) => {
      const child = childProcess.spawn(file, args, { cwd: destination, stdio: "ignore",
        env: sanitizedEnvironment(path.join(destination, ".home")), timeout: 180_000 });
      child.on("error", () => reject(new RuntimeFailure("Package installer executable is unavailable")));
      child.on("exit", (code) => code === 0 ? resolve()
        : reject(new RuntimeFailure("Package installation failed; verify pinned packages and runtime availability")));
    });
    try {
      if (spec.runtime === "python") {
        await run("python3", ["-m", "venv", destination]);
        await run(path.join(bin, "python"), ["-m", "pip", "--isolated", "--disable-pip-version-check", "install",
          "--no-input", "--no-deps", "--only-binary=:all:", "--index-url", "https://pypi.org/simple", ...requirements]);
      } else {
        await run("npm", ["install", "--prefix", destination, "--save-exact", "--ignore-scripts", "--no-audit",
          "--no-fund", "--package-lock", "--registry=https://registry.npmjs.org", ...requirements]);
      }
      if (!(await exists(command[0]))) throw new RuntimeFailure("Installed package does not provide its approved entrypoint");
      await fs.writeFile(marker, key, { mode: 0o600 });
      return command;
    } catch (error) {
      await fs.rm(destination, { recursive: true, force: true });
      throw error;
    }
  }

  forget(session: Session): void {
    this.sessions.delete(session);
  }

  async close(): Promise<void> {
    this.closed = true;
    const sessions = [...this.sessions];
    this.sessions.clear();
    await Promise.all(sessions.map((s) => s.close()));
  }
}
