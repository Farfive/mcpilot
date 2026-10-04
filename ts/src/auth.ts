/**
 * Host-owned credentials and policy-bounded MCP OAuth (official TypeScript SDK).
 *
 * Login is non-blocking: when a server needs authorization, the provider records
 * a pending login and notifies the host UI (LoginBroker). The host's callback
 * route calls `broker.complete()`, which redeems the code; the next `connect()`
 * or workflow `resume()` then succeeds. Nothing here reaches a model or a log.
 */

import {
  StreamableHTTPClientTransport,
  type OAuthClientMetadata,
  type OAuthClientProvider,
  type OAuthDiscoveryState,
  type StoredOAuthClientInformation,
  type StoredOAuthTokens,
} from "@modelcontextprotocol/client";

import { canonicalJson, sha256Hex } from "./canonical.js";
import { AuthFailure, AuthRequired } from "./errors.js";
import type { FetchLike } from "./catalog.js";
import type { Integration } from "./models.js";
import { compareCodePoints } from "./router.js";

export interface CredentialKey {
  user_id: string;
  integration_id: string;
  account: string;
  endpoint: string;
  digest: string;
}

/** Same digest as Python `CredentialKey.digest` (RFC 8785 canonical form). */
export function credentialKey(userId: string, integrationId: string, account: string, endpoint: string): CredentialKey {
  return { user_id: userId, integration_id: integrationId, account, endpoint,
           digest: sha256Hex(canonicalJson([userId, integrationId, account, endpoint])) };
}

export const connectionIdFor = (key: CredentialKey) => `conn_${key.digest.slice(0, 24)}`;

export type SecretRecord = Record<string, unknown>;

/** Implement over a KMS, Vault or application database for multi-instance hosts. */
export interface SecretStore {
  get(key: CredentialKey): Promise<SecretRecord | undefined>;
  set(key: CredentialKey, value: SecretRecord): Promise<void>;
  delete(key: CredentialKey): Promise<void>;
}

export class MemorySecretStore implements SecretStore {
  private readonly values = new Map<string, SecretRecord>();

  async get(key: CredentialKey) {
    const value = this.values.get(key.digest);
    return value === undefined ? undefined : structuredClone(value);
  }

  async set(key: CredentialKey, value: SecretRecord) {
    this.values.set(key.digest, structuredClone(value));
  }

  async delete(key: CredentialKey) {
    this.values.delete(key.digest);
  }
}

/** Host UI event. Never place its URL in an LLM prompt or audit payload. */
export interface AuthorizationRequest {
  connectionId: string;
  integrationId: string;
  account: string;
  scopes: string[];
  url: string;
  userId: string;
}

interface PendingLogin {
  request: AuthorizationRequest;
  finish: (params: URLSearchParams) => Promise<void>;
  expires: number;
}

/**
 * Turns pending logins into "Connect" buttons and redeems the provider redirect.
 * `complete()` accepts only a known `state` belonging to the same host user.
 */
export class LoginBroker {
  private readonly logins = new Map<string, PendingLogin>();

  constructor(
    private readonly notify?: (request: AuthorizationRequest) => void | Promise<void>,
    private readonly options: { timeoutMs?: number; maxPending?: number } = {},
  ) {}

  get timeoutMs(): number {
    return this.options.timeoutMs ?? 600_000;
  }

  pending(userId: string): AuthorizationRequest[] {
    this.expire();
    return [...this.logins.values()].filter((l) => l.request.userId === userId).map((l) => ({ ...l.request }));
  }

  /** Called by the OAuth provider; hosts do not call this directly. */
  async begin(request: AuthorizationRequest, state: string, finish: PendingLogin["finish"]): Promise<void> {
    this.expire();
    for (const [key, login] of this.logins) {
      if (login.request.userId === request.userId && login.request.connectionId === request.connectionId) {
        this.logins.delete(key); // a newer attempt supersedes the old button
      }
    }
    if (this.logins.size >= (this.options.maxPending ?? 1000)) throw new AuthRequired("Too many pending logins");
    this.logins.set(state, { request, finish, expires: Date.now() + this.timeoutMs });
    try {
      await this.notify?.({ ...request });
    } catch (error) {
      this.logins.delete(state); // no button reached the user
      throw error;
    }
  }

  /** Host callback route: pass the full redirect URL. Returns false for unknown state or another user. */
  async complete({ userId, callbackUrl }: { userId: string; callbackUrl: string }): Promise<boolean> {
    this.expire();
    const params = new URL(callbackUrl).searchParams;
    const state = params.get("state") ?? "";
    const login = this.logins.get(state);
    if (!login || login.request.userId !== userId) return false;
    this.logins.delete(state);
    if (params.get("error") || !params.get("code")) return false;
    try {
      await login.finish(params);
      return true;
    } catch {
      return false;
    }
  }

  cancel(userId: string, connectionId: string): boolean {
    for (const [key, login] of this.logins) {
      if (login.request.userId === userId && login.request.connectionId === connectionId) {
        this.logins.delete(key);
        return true;
      }
    }
    return false;
  }

  hasPending(userId: string, connectionId: string): boolean {
    return this.pending(userId).some((r) => r.connectionId === connectionId);
  }

  private expire() {
    const now = Date.now();
    for (const [key, login] of this.logins) if (login.expires < now) this.logins.delete(key);
  }
}

export interface OAuthConfig {
  redirectUri?: string;
  clientId?: string;
  clientSecret?: string;
  issuer?: string;
  clientMetadataUrl?: string;
  scopes?: string[];
  login?: LoginBroker;
  allowedAuthHosts?: string[];
  tokenEndpointAuthMethod?: string;
}

export interface AuthHandle {
  connectionId: string;
  status: "ready" | "auth_required" | "setup_required";
  capabilities: string[];
  reason: string | null;
  headers: Record<string, string>;
  env: Record<string, string>;
  authProvider?: OAuthClientProvider;
  fetch?: FetchLike;
}

const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

/**
 * Restricts OAuth and MCP requests to HTTPS (loopback only for loopback endpoints) and approved hosts.
 * With `scopes`, Protected Resource Metadata advertises only the policy scopes, so the SDK requests
 * the minimal set instead of everything the server supports.
 */
export function guardedFetch(endpoint: string, allowedAuthHosts: string[] = [], base: FetchLike = fetch,
                             scopes?: string[]): FetchLike {
  const server = new URL(endpoint);
  return async (input, init) => {
    const target = new URL(input instanceof Request ? input.url : String(input));
    const loopback = target.protocol === "http:" && LOOPBACK.has(target.hostname) && LOOPBACK.has(server.hostname);
    if (target.protocol !== "https:" && !loopback) {
      throw new AuthFailure("OAuth requires HTTPS except for explicit loopback development endpoints.");
    }
    if (allowedAuthHosts.length && ![server.hostname, ...allowedAuthHosts].includes(target.hostname)) {
      throw new AuthFailure("OAuth discovery requested an unapproved authorization host.");
    }
    const response = await base(input, init);
    if (!scopes || !response.ok || !target.pathname.startsWith("/.well-known/oauth-protected-resource")) return response;
    const metadata = await response.json() as Record<string, unknown>;
    if (scopes.length) metadata.scopes_supported = scopes;
    else delete metadata.scopes_supported;
    const headers = new Headers(response.headers);
    headers.delete("content-length");
    return new Response(JSON.stringify(metadata), { status: response.status, headers });
  };
}

// Refresh tokens need `offline_access` at many providers; it grants no data access.
const IMPLICIT_SCOPES = new Set(["offline_access"]);

function randomState(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

class StoreOAuthProvider implements OAuthClientProvider {
  private currentState: string | null = null;

  constructor(
    private readonly store: SecretStore,
    private readonly key: CredentialKey,
    private readonly scopes: string[],
    private readonly config: OAuthConfig,
    private readonly connectionId: string,
    private readonly finish: (provider: StoreOAuthProvider, params: URLSearchParams) => Promise<void>,
  ) {}

  get redirectUrl(): string {
    return this.config.redirectUri ?? "http://127.0.0.1:8765/callback";
  }

  get clientMetadataUrl(): string | undefined {
    return this.config.clientMetadataUrl;
  }

  get clientMetadata(): OAuthClientMetadata {
    return {
      client_name: "MCPilot",
      redirect_uris: [this.redirectUrl],
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      token_endpoint_auth_method: this.config.tokenEndpointAuthMethod ?? "none",
      // Registration and authorization request only the scopes of this plan.
      ...(this.scopes.length ? { scope: this.scopes.join(" ") } : {}),
    } as OAuthClientMetadata;
  }

  private async record(): Promise<SecretRecord> {
    return (await this.store.get(this.key)) ?? {};
  }

  private async update(change: (record: SecretRecord) => void): Promise<void> {
    const record = await this.record();
    change(record);
    await this.store.set(this.key, record);
  }

  state(): string {
    this.currentState = randomState();
    return this.currentState;
  }

  async clientInformation(): Promise<StoredOAuthClientInformation | undefined> {
    return (await this.record()).client_info as StoredOAuthClientInformation | undefined;
  }

  async saveClientInformation(info: StoredOAuthClientInformation): Promise<void> {
    await this.update((r) => { r.client_info = info; });
  }

  private granted(tokens: { scope?: string }): boolean {
    const scope = new Set((tokens.scope ?? "").split(" ").filter(Boolean));
    return this.scopes.every((s) => scope.has(s));
  }

  async tokens(): Promise<StoredOAuthTokens | undefined> {
    const tokens = (await this.record()).tokens as StoredOAuthTokens | undefined;
    return tokens && this.granted(tokens) ? tokens : undefined;
  }

  async saveTokens(tokens: StoredOAuthTokens): Promise<void> {
    if (!this.granted(tokens)) throw new AuthRequired("Provider did not grant the requested capabilities.");
    await this.update((r) => { r.tokens = tokens; delete r.verifier; });
  }

  async redirectToAuthorization(url: URL): Promise<void> {
    const requested = (url.searchParams.get("scope") ?? "").split(" ").filter(Boolean);
    if (!requested.every((s) => this.scopes.includes(s) || IMPLICIT_SCOPES.has(s))) {
      throw new AuthRequired("Additional authorization requires a policy update.");
    }
    if (!this.config.login || !this.currentState) throw new AuthRequired("Connect the account through the host UI.");
    await this.config.login.begin({
      connectionId: this.connectionId, integrationId: this.key.integration_id, account: this.key.account,
      scopes: [...this.scopes], url: url.href, userId: this.key.user_id,
    }, this.currentState, (params) => this.finish(this, params));
  }

  async saveCodeVerifier(verifier: string): Promise<void> {
    await this.update((r) => { r.verifier = verifier; });
  }

  async codeVerifier(): Promise<string> {
    const verifier = (await this.record()).verifier;
    if (typeof verifier !== "string") throw new AuthRequired("Login was not started; reconnect the account.");
    return verifier;
  }

  async saveDiscoveryState(state: OAuthDiscoveryState): Promise<void> {
    await this.update((r) => { r.discovery = state; });
  }

  async discoveryState(): Promise<OAuthDiscoveryState | undefined> {
    return (await this.record()).discovery as OAuthDiscoveryState | undefined;
  }

  async invalidateCredentials(scope: "all" | "client" | "tokens" | "verifier" | "discovery"): Promise<void> {
    if (scope === "all") return this.store.delete(this.key);
    const field = { client: "client_info", tokens: "tokens", verifier: "verifier", discovery: "discovery" }[scope];
    await this.update((r) => { delete r[field]; });
  }
}

export class AuthManager {
  private readonly oauthConfigs = new Map<string, OAuthConfig>();

  constructor(readonly store: SecretStore = new MemorySecretStore(), private readonly defaultOAuth?: OAuthConfig) {}

  configureOAuth(integrationId: string, config: OAuthConfig): void {
    this.oauthConfigs.set(integrationId, config);
  }

  async setSecret(userId: string, integrationId: string, account: string, secret: string, endpoint: string): Promise<void> {
    if (!secret || /[\r\n]/.test(secret)) throw new AuthFailure("A nonempty single-line credential is required.");
    await this.store.set(credentialKey(userId, integrationId, account, endpoint), { secret });
  }

  async prepare(userId: string, integration: Integration, account: string, capabilities: string[]): Promise<AuthHandle> {
    const key = credentialKey(userId, integration.id, account, integration.endpoint ?? "stdio");
    const connectionId = connectionIdFor(key);
    const caps = [...new Set(capabilities)].sort(compareCodePoints);
    const handle: AuthHandle = { connectionId, status: "ready", capabilities: caps, reason: null, headers: {}, env: {} };
    const blocked = (status: "auth_required" | "setup_required", reason: string): AuthHandle =>
      ({ ...handle, status, capabilities: [], reason });
    const spec = integration.auth;
    if (spec.mode === "none") return handle;
    const record = (await this.store.get(key)) ?? {};
    if (spec.mode === "bearer" || spec.mode === "api_key") {
      const secret = record.secret;
      if (typeof secret !== "string" || !secret) {
        return spec.setup_required ? blocked("setup_required", "Configure the provider application.")
          : blocked("auth_required", "Connect the account through the host UI.");
      }
      if (integration.transport === "stdio") {
        if (!spec.env_var) return blocked("setup_required", "Configure the credential environment variable.");
        handle.env[spec.env_var] = secret;
      } else {
        handle.headers[spec.header_name] = spec.mode === "bearer" ? `Bearer ${secret}` : secret;
      }
      return handle;
    }
    if (integration.transport !== "http" || !integration.endpoint) {
      return blocked("setup_required", "This authentication adapter is not supported.");
    }
    let config = this.oauthConfigs.get(integration.id) ?? this.defaultOAuth;
    if (!config) {
      if (spec.setup_required) return blocked("setup_required", "Configure the provider OAuth application.");
      config = {}; // stored grants remain usable without a login UI; a new login is not
    }
    if (config.clientSecret && !config.clientId) return blocked("setup_required", "OAuth client configuration is incomplete.");
    const scopes = [...new Set([...caps.flatMap((c) => spec.scopes_by_capability[c] ?? []), ...(config.scopes ?? [])])]
      .sort(compareCodePoints);
    const fetchFn = guardedFetch(integration.endpoint, config.allowedAuthHosts, fetch, scopes);
    const endpoint = integration.endpoint;
    const provider = new StoreOAuthProvider(this.store, key, scopes, config, connectionId, async (p, params) => {
      // Code redemption (PKCE, resource, RFC 9207 issuer check) by the official SDK.
      await new StreamableHTTPClientTransport(new URL(endpoint), { authProvider: p, fetch: fetchFn }).finishAuth(params);
    });
    if (!(await provider.tokens()) && !config.login) {
      return blocked("auth_required", "Connect the account through the host UI.");
    }
    if (config.clientId && !record.client_info) {
      if (!config.issuer) return blocked("setup_required", "Bind preregistered OAuth credentials to their issuer.");
      await provider.saveClientInformation({
        client_id: config.clientId, ...(config.clientSecret ? { client_secret: config.clientSecret } : {}),
        redirect_uris: [provider.redirectUrl], issuer: config.issuer,
      } as StoredOAuthClientInformation);
    }
    return { ...handle, authProvider: provider, fetch: fetchFn };
  }

  /** Forget local grants; optionally revoke at the provider through a host adapter. */
  async revoke(userId: string, integrationId: string, account: string, endpoint: string | null,
               providerRevoker?: (token: string) => Promise<void>): Promise<void> {
    const key = credentialKey(userId, integrationId, account, endpoint ?? "stdio");
    const record = (await this.store.get(key)) ?? {};
    await this.store.delete(key);
    const tokens = (record.tokens ?? {}) as { refresh_token?: string; access_token?: string };
    const token = tokens.refresh_token ?? tokens.access_token ?? (record.secret as string | undefined);
    if (token && providerRevoker) {
      try {
        await providerRevoker(token);
      } catch {
        throw new AuthFailure("Local access was removed; provider revocation did not complete.");
      }
    }
  }
}
