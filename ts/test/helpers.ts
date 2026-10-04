import { spawn, type ChildProcess } from "node:child_process";
import { readFileSync } from "node:fs";
import { mkdtemp } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

/** Repository root (compiled file lives in ts/dist/test). */
export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
export const PYTHON = process.env.MCPILOT_PYTHON ?? join(ROOT, ".venv", "bin", "python");
export const RUNTIME_SERVER = join(ROOT, "tests", "runtime_server.py");
export const vectors = JSON.parse(readFileSync(join(ROOT, "spec", "vectors.json"), "utf8"));

export const tempDir = () => mkdtemp(join(tmpdir(), "mcpilot-ts-"));

export async function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const server = createServer().listen(0, "127.0.0.1", () => {
      const port = (server.address() as { port: number }).port;
      server.close(() => resolve(port));
    });
  });
}

/** Start ts/test/fixtures/serve.py and read its first JSON line. */
export async function pythonFixture(mode: "oauth" | "gateway"): Promise<{ info: Record<string, string>; stop: () => Promise<void> }> {
  const child = spawn(PYTHON, [join(ROOT, "ts", "test", "fixtures", "serve.py"), mode],
    { cwd: ROOT, stdio: ["pipe", "pipe", "inherit"] });
  const line = await new Promise<string>((resolve, reject) => {
    createInterface({ input: child.stdout! }).once("line", resolve);
    child.once("exit", (code) => reject(new Error(`fixture exited with ${code}`)));
  });
  return { info: JSON.parse(line), stop: () => stop(child) };
}

export async function stop(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null) return;
  child.stdin?.end();
  const exited = new Promise((resolve) => child.once("exit", resolve));
  const timer = setTimeout(() => child.kill("SIGKILL"), 5000);
  await exited;
  clearTimeout(timer);
}

export async function waitForPort(port: number, child: ChildProcess): Promise<void> {
  const { connect } = await import("node:net");
  for (let i = 0; i < 300; i++) {
    if (child.exitCode !== null) throw new Error("server exited");
    const ok = await new Promise<boolean>((resolve) => {
      const socket = connect(port, "127.0.0.1", () => { socket.end(); resolve(true); });
      socket.on("error", () => resolve(false));
    });
    if (ok) return;
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error("server did not start");
}
