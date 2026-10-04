/**
 * Trusted executable manifests and separate, non-executable registry discovery.
 * Registry v0.1 (generic registry API); same semantics as the Python `Catalog`.
 */

import { clone, deepFreeze, parseIntegration, type Integration } from "./models.js";

export const OFFICIAL_REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers";
const REGISTRY_META = "io.modelcontextprotocol.registry/official";
const MAX_PAGE_BYTES = 2_000_000;
const OVERLAP_MS = 5 * 60 * 1000;
// Python str.isprintable() is False for these categories (space excepted).
const NON_PRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;

export interface RegistrySummary {
  id: string;
  version: string;
  description: string;
  publisher_namespace: string;
  transports: string[];
  registry_status: "active" | "deprecated" | "deleted";
  updated_at: string;
  support: "discovered";
  untrusted: true;
}

interface Snapshot {
  servers: RegistrySummary[];
  last_sync: string | null;
  last_full_sync: string | null;
  last_attempt: string | null;
  error: string | null;
}

/** Optional persistence for discovery snapshots (e.g. a JSON file or a key-value store). */
export interface SnapshotStore {
  load(): Promise<unknown>;
  save(data: unknown): Promise<void>;
}

export type FetchLike = (input: string | URL, init?: RequestInit) => Promise<Response>;

function text(value: unknown, limit: number): string {
  if (typeof value !== "string") return "";
  const chars = Array.from(value).slice(0, limit * 2).filter((c) => c === " " || !NON_PRINTABLE.test(c));
  return Array.from(chars.join("").split(" ").filter(Boolean).join(" ")).slice(0, limit).join("");
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Bounded, informational summary; no launch commands, endpoints or packages survive. */
export function summarize(entry: unknown): RegistrySummary {
  if (!isObject(entry) || !isObject(entry.server)) throw new TypeError("Invalid registry server envelope");
  const server = entry.server;
  const name = text(server.name, 256);
  const version = text(server.version, 128);
  if (!name || !version) throw new TypeError("Registry entries require name and version");
  const meta = "_meta" in entry ? entry._meta : {};
  if (!isObject(meta)) throw new TypeError("Invalid registry metadata");
  const official = REGISTRY_META in meta ? meta[REGISTRY_META] : {};
  if (!isObject(official)) throw new TypeError("Invalid registry lifecycle metadata");
  const status = "status" in official ? official.status : "active";
  if (status !== "active" && status !== "deprecated" && status !== "deleted") {
    throw new TypeError("Unknown registry lifecycle status");
  }
  const transports = new Set<string>();
  for (const remote of Array.isArray(server.remotes) ? server.remotes : []) {
    if (isObject(remote) && (remote.type === "streamable-http" || remote.type === "sse")) transports.add(remote.type);
  }
  for (const pkg of Array.isArray(server.packages) ? server.packages : []) {
    if (isObject(pkg) && isObject(pkg.transport) && pkg.transport.type === "stdio") transports.add("stdio");
  }
  return {
    id: name,
    version,
    description: text(server.description, 500),
    publisher_namespace: name.split("/")[0],
    transports: [...transports].sort(),
    registry_status: status,
    updated_at: text("updatedAt" in official ? official.updatedAt : official.publishedAt, 64),
    support: "discovered",
    untrusted: true,
  };
}

function sourceUrl(source: string): string {
  const url = new URL(source);
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  if (url.username || url.password || url.search || url.hash || !(url.protocol === "https:" || (url.protocol === "http:" && loopback))) {
    throw new TypeError("Registry sources require HTTPS, no credentials/query/fragment");
  }
  const base = source.replace(/\/+$/, "");
  if (base.endsWith("/v0.1/servers")) return base;
  return base.endsWith("/v0.1") ? `${base}/servers` : `${base}/v0.1/servers`;
}

export interface SyncOptions {
  sources?: string[];
  fetch?: FetchLike;
  maxPages?: number;
  pageSize?: number;
  fullRefreshAfterMs?: number;
  signal?: AbortSignal;
}

export class Catalog {
  private readonly trusted = new Map<string, Integration>();
  private snapshots: Record<string, Snapshot> = {};
  private syncing: Promise<unknown> = Promise.resolve();
  cacheError: string | null = null;

  constructor(integrations: Iterable<unknown> = [], private readonly store?: SnapshotStore) {
    for (const integration of integrations) this.add(integration);
  }

  /** Add or replace a manifest supplied by trusted host code (validated and frozen). */
  add(integration: unknown): Integration {
    const parsed = deepFreeze(parseIntegration(clone(integration)));
    this.trusted.set(parsed.id, parsed);
    return parsed;
  }

  get(id: string): Integration {
    const found = this.trusted.get(id);
    if (!found) throw new RangeError(`Unknown integration: ${id}`);
    return found;
  }

  has(id: string): boolean {
    return this.trusted.has(id);
  }

  all(): Integration[] {
    return [...this.trusted.values()];
  }

  /** Validate a host-owned manifest list (or `{"integrations": [...]}`) atomically. */
  loadManifest(data: unknown): Integration[] {
    const list = isObject(data) && Object.keys(data).length === 1 && Array.isArray(data.integrations)
      ? data.integrations : data;
    if (!Array.isArray(list)) throw new TypeError("A trusted manifest must contain a list of integrations");
    const parsed = list.map((item) => parseIntegration(clone(item)));
    if (new Set(parsed.map((i) => i.id)).size !== parsed.length) {
      throw new TypeError("A manifest cannot contain duplicate integration IDs");
    }
    return parsed.map((item) => this.add(item));
  }

  async loadCache(): Promise<void> {
    if (!this.store) return;
    try {
      const data = await this.store.load();
      if (data === undefined) return;
      if (!isObject(data) || data.format_version !== 1 || !isObject(data.sources)) throw new TypeError("Invalid cache");
      const validated: Record<string, Snapshot> = {};
      for (const [source, snapshot] of Object.entries(data.sources)) {
        if (sourceUrl(source) !== source || !isObject(snapshot) || !Array.isArray(snapshot.servers)) {
          throw new TypeError("Invalid cached source");
        }
        validated[source] = {
          servers: snapshot.servers.map((s) => {
            const item = s as Record<string, unknown>;
            const kinds = Array.isArray(item.transports) ? item.transports : [];
            return summarize({
              server: { name: item.id, version: item.version, description: item.description ?? "",
                        remotes: kinds.map((t) => ({ type: t })), packages: kinds.map((t) => ({ transport: { type: t } })) },
              _meta: { [REGISTRY_META]: { status: item.registry_status, updatedAt: item.updated_at ?? "" } },
            });
          }),
          last_sync: text(snapshot.last_sync, 64) || null,
          last_full_sync: text(snapshot.last_full_sync, 64) || null,
          last_attempt: text(snapshot.last_attempt, 64) || null,
          error: text(snapshot.error, 128) || null,
        };
      }
      this.snapshots = validated;
    } catch {
      this.cacheError = "invalid_cache";
    }
  }

  discovered(limit = 20): RegistrySummary[] {
    if (limit < 0 || limit > 1000) throw new RangeError("Discovery limit must be between 0 and 1000");
    const found: (RegistrySummary & { source: string })[] = [];
    for (const source of Object.keys(this.snapshots).sort()) {
      for (const item of this.snapshots[source].servers) {
        if (item.registry_status !== "deleted") found.push({ ...item, source });
      }
    }
    return clone(found.slice(0, limit));
  }

  syncStatus(): Record<string, Omit<Snapshot, "servers"> & { count: number }> {
    return Object.fromEntries(Object.entries(this.snapshots).map(([source, s]) => [source, {
      last_sync: s.last_sync, last_full_sync: s.last_full_sync, last_attempt: s.last_attempt, error: s.error,
      count: s.servers.length,
    }]));
  }

  /** Full snapshot first, then `updated_since` increments; commit only complete page sets. */
  async sync(options: SyncOptions = {}): Promise<ReturnType<Catalog["syncStatus"]>> {
    const maxPages = options.maxPages ?? 1000;
    const pageSize = options.pageSize ?? 100;
    if (maxPages < 1 || maxPages > 10_000 || pageSize < 1 || pageSize > 100) {
      throw new RangeError("Invalid registry pagination limits");
    }
    const sources = [...new Set((options.sources ?? [OFFICIAL_REGISTRY]).map(sourceUrl))];
    const run = this.syncing.then(async () => {
      for (const source of sources) await this.syncSource(source, options, maxPages, pageSize);
    });
    this.syncing = run.catch(() => undefined);
    await run;
    return this.syncStatus();
  }

  private async syncSource(source: string, options: SyncOptions, maxPages: number, pageSize: number): Promise<void> {
    const started = Date.now();
    const attempt = new Date(started).toISOString();
    const previous = this.snapshots[source] ?? { servers: [], last_sync: null, last_full_sync: null, last_attempt: null, error: null };
    const fullAfter = options.fullRefreshAfterMs ?? 86_400_000;
    const last = previous.last_sync ? Date.parse(previous.last_sync) : NaN;
    const full = previous.last_full_sync ? Date.parse(previous.last_full_sync) : NaN;
    const incremental = Number.isFinite(last) && Number.isFinite(full) && started - full < fullAfter && last <= started;
    let candidate: Snapshot;
    try {
      const fetched = await this.fetchPages(source, options, maxPages, pageSize,
        incremental ? new Date(last - OVERLAP_MS).toISOString() : null);
      const merged = new Map(incremental ? previous.servers.map((s) => [s.id, s] as const) : []);
      for (const item of fetched) merged.set(item.id, item);
      candidate = {
        servers: [...merged.keys()].sort().map((key) => merged.get(key)!),
        last_sync: attempt, last_full_sync: incremental ? previous.last_full_sync : attempt,
        last_attempt: attempt, error: null,
      };
    } catch (error) {
      // Error messages may contain URLs, cursors or headers: keep only the class name.
      candidate = { ...previous, last_attempt: attempt, error: (error as Error)?.name ?? "Error" };
    }
    const updated = { ...this.snapshots, [source]: candidate };
    if (this.store) await this.store.save({ format_version: 1, sources: updated });
    this.snapshots = updated;
  }

  private async fetchPages(source: string, options: SyncOptions, maxPages: number, pageSize: number,
                           updatedSince: string | null): Promise<RegistrySummary[]> {
    const fetchFn = options.fetch ?? fetch;
    const servers = new Map<string, RegistrySummary>();
    const seen = new Set<string>();
    let cursor: string | null = null;
    for (let page = 0; page < maxPages; page++) {
      const url = new URL(source);
      url.searchParams.set("version", "latest");
      url.searchParams.set("limit", String(pageSize));
      url.searchParams.set("include_deleted", "true");
      if (updatedSince) url.searchParams.set("updated_since", updatedSince);
      if (cursor) url.searchParams.set("cursor", cursor);
      const response = await fetchFn(url, { redirect: "error", signal: options.signal ?? AbortSignal.timeout(20_000) });
      if (!response.ok) throw Object.assign(new Error("Registry request failed"), { name: "HTTPStatusError" });
      const body = await response.text();
      if (new TextEncoder().encode(body).length > MAX_PAGE_BYTES) throw new RangeError("Registry page exceeds size limit");
      const data: unknown = JSON.parse(body);
      if (!isObject(data) || !Array.isArray(data.servers)) throw new TypeError("Invalid registry list response");
      if (data.servers.length > pageSize) throw new RangeError("Registry returned more entries than requested");
      for (const entry of data.servers) {
        const item = summarize(entry);
        if (servers.has(item.id)) throw new TypeError("Registry snapshot contains duplicate latest IDs");
        servers.set(item.id, item);
      }
      const metadata = data.metadata ?? {};
      if (!isObject(metadata)) throw new TypeError("Invalid pagination metadata");
      const next = metadata.nextCursor;
      if (next === undefined || next === null || next === "") return [...servers.values()];
      if (typeof next !== "string" || next.length > 4096 || seen.has(next)) {
        throw new TypeError("Invalid or repeated registry cursor");
      }
      seen.add(next);
      cursor = next;
    }
    throw new RangeError("Registry pagination exceeded maxPages; snapshot not committed");
  }
}
