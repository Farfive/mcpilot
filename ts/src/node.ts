/**
 * Node-only persistence: Fernet-encrypted credentials (file format shared with the
 * Python `EncryptedFileSecretStore`), file-backed workflows and catalog snapshots.
 */

import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { appendFile, mkdir, open, readFile, rename, rm, unlink } from "node:fs/promises";
import { dirname } from "node:path";
import { join } from "node:path";

import type { CredentialKey, SecretRecord, SecretStore } from "./auth.js";
import type { SnapshotStore } from "./catalog.js";
import { AuthFailure, MCPilotError } from "./errors.js";
import type { AuditEvent } from "./models.js";
import type { WorkflowRun, WorkflowStore } from "./workflow.js";

const b64url = (data: Buffer) => data.toString("base64").replace(/\+/g, "-").replace(/\//g, "_");
const fromB64url = (text: string) => Buffer.from(text.replace(/-/g, "+").replace(/_/g, "/"), "base64");

/** Fernet (spec v0x80): AES-128-CBC + HMAC-SHA256, interoperable with Python `cryptography`. */
export class Fernet {
  private readonly signing: Buffer;
  private readonly encryption: Buffer;

  constructor(key: string | Uint8Array) {
    const raw = typeof key === "string" ? fromB64url(key.trim()) : Buffer.from(key);
    if (raw.length !== 32) throw new TypeError("Fernet key must be 32 url-safe base64-encoded bytes");
    this.signing = raw.subarray(0, 16);
    this.encryption = raw.subarray(16);
  }

  static generateKey(): string {
    return b64url(randomBytes(32));
  }

  encrypt(plaintext: Uint8Array, now = Date.now(), iv: Uint8Array = randomBytes(16)): string {
    const header = Buffer.alloc(9);
    header[0] = 0x80;
    header.writeBigUInt64BE(BigInt(Math.floor(now / 1000)), 1);
    const cipher = createCipheriv("aes-128-cbc", this.encryption, iv);
    const body = Buffer.concat([header, Buffer.from(iv), cipher.update(plaintext), cipher.final()]);
    return b64url(Buffer.concat([body, createHmac("sha256", this.signing).update(body).digest()]));
  }

  decrypt(token: string): Buffer {
    const data = fromB64url(token.trim());
    if (data.length < 57 || data[0] !== 0x80) throw new TypeError("Invalid Fernet token");
    const body = data.subarray(0, data.length - 32);
    const expected = createHmac("sha256", this.signing).update(body).digest();
    if (!timingSafeEqual(expected, data.subarray(data.length - 32))) throw new TypeError("Invalid Fernet token");
    const decipher = createDecipheriv("aes-128-cbc", this.encryption, body.subarray(9, 25));
    return Buffer.concat([decipher.update(body.subarray(25)), decipher.final()]);
  }
}

async function atomicWrite(path: string, data: string): Promise<void> {
  const temporary = `${path}.${randomBytes(8).toString("hex")}.tmp`;
  const handle = await open(temporary, "w", 0o600);
  try {
    await handle.writeFile(data);
    await handle.sync();
  } finally {
    await handle.close();
  }
  try {
    await rename(temporary, path);
  } catch (error) {
    await rm(temporary, { force: true });
    throw error;
  }
}

/** One encrypted `<digest>.enc` file per credential, mode 0600; readable by the Python SDK with the same key. */
export class EncryptedFileSecretStore implements SecretStore {
  private readonly fernet: Fernet;

  constructor(private readonly directory: string, key: string | Uint8Array) {
    this.fernet = new Fernet(key);
  }

  private path(key: CredentialKey) {
    return join(this.directory, `${key.digest}.enc`);
  }

  async get(key: CredentialKey): Promise<SecretRecord | undefined> {
    let token: string;
    try {
      token = await readFile(this.path(key), "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
      throw new AuthFailure("Credential storage could not be read.");
    }
    try {
      return JSON.parse(this.fernet.decrypt(token).toString("utf8"));
    } catch {
      throw new AuthFailure("Credential storage could not be decrypted.");
    }
  }

  async set(key: CredentialKey, value: SecretRecord): Promise<void> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    await atomicWrite(this.path(key), this.fernet.encrypt(Buffer.from(JSON.stringify(value))));
  }

  async delete(key: CredentialKey): Promise<void> {
    await rm(this.path(key), { force: true });
  }
}

const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
};

/** Host-owned local workflow state, including tool results; use protected storage in production. */
export class JsonFileWorkflowStore implements WorkflowStore {
  constructor(private readonly directory: string) {}

  private file(runId: string, suffix: string) {
    return join(this.directory, createHash("sha256").update(runId).digest("hex") + suffix);
  }

  async save(run: WorkflowRun): Promise<void> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    try {
      const existing = JSON.parse(await readFile(this.file(run.id, ".json"), "utf8")) as WorkflowRun;
      if (existing.user_id !== run.user_id) return;
    } catch {
      // New run.
    }
    await atomicWrite(this.file(run.id, ".json"), JSON.stringify(run));
  }

  async load(runId: string, userId: string): Promise<WorkflowRun> {
    try {
      const run = JSON.parse(await readFile(this.file(runId, ".json"), "utf8")) as WorkflowRun;
      if (run.user_id === userId) return run;
    } catch {
      // Fall through to the uniform error.
    }
    throw new MCPilotError("Workflow is not available to this user");
  }

  async lock(runId: string): Promise<() => Promise<void>> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    const path = this.file(runId, ".lock");
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const handle = await open(path, "wx", 0o600);
        await handle.writeFile(String(process.pid));
        await handle.close();
        return async () => { await unlink(path).catch(() => undefined); };
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        const owner = Number(await readFile(path, "utf8").catch(() => "0"));
        if (owner && alive(owner)) break;
        await unlink(path).catch(() => undefined); // stale lock from a crashed process
      }
    }
    throw new MCPilotError("Workflow is already executing");
  }
}

/** Atomic JSON file for registry discovery snapshots (untrusted data, no secrets). */
export class JsonFileSnapshotStore implements SnapshotStore {
  constructor(private readonly path: string) {}

  async load(): Promise<unknown> {
    try {
      return JSON.parse(await readFile(this.path, "utf8"));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
      throw error;
    }
  }

  async save(data: unknown): Promise<void> {
    await mkdir(join(this.path, ".."), { recursive: true, mode: 0o700 });
    await atomicWrite(this.path, JSON.stringify(data));
  }
}


/** Append-only JSON Lines audit log (mode 0600), same record shape as the Python `JsonlAuditSink`. */
export class JsonlAuditSink {
  private queue: Promise<void> = Promise.resolve();

  constructor(private readonly path: string) {}

  readonly write = (event: AuditEvent): Promise<void> => {
    this.queue = this.queue.then(async () => {
      await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
      await appendFile(this.path, JSON.stringify(event) + "\n", { mode: 0o600 });
    });
    return this.queue;
  };
}
