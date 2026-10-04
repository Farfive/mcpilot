/** Host decisions as immutable snapshots, independent of registry or model content. */

import { canonicalJson, sha256Hex } from "./canonical.js";
import { PolicyDenied } from "./errors.js";
import type { Effect, Integration, ToolRule } from "./models.js";

/** SHA-256 of the RFC 8785 canonical manifest; identical in the Python SDK. */
export function fingerprint(integration: Integration): string {
  return sha256Hex(canonicalJson(integration));
}

// IANA special-purpose ranges, mirroring Python 3.13 `ipaddress` is_global semantics.
const V4_PRIVATE = ["0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
  "192.0.0.170/31", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
  "240.0.0.0/4", "255.255.255.255/32"];
const V4_EXCEPTIONS = ["192.0.0.9/32", "192.0.0.10/32"];
const V4_SHARED = "100.64.0.0/10";
const V6_PRIVATE = ["::1/128", "::/128", "::ffff:0:0/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32",
  "2002::/16", "3fff::/20", "fc00::/7", "fe80::/10"];
const V6_EXCEPTIONS = ["2001:1::1/128", "2001:1::2/128", "2001:3::/32", "2001:4:112::/48", "2001:20::/28",
  "2001:30::/28"];

type Address = { version: 4 | 6; value: bigint };

function parseV4(text: string): bigint | null {
  const parts = text.split(".");
  if (parts.length !== 4 || !parts.every((p) => /^(0|[1-9][0-9]{0,2})$/.test(p) && Number(p) <= 255)) return null;
  return parts.reduce((acc, p) => (acc << 8n) | BigInt(p), 0n);
}

function parseV6(text: string): bigint | null {
  let head = text;
  const v4 = /(.*:)(\d+\.\d+\.\d+\.\d+)$/.exec(text);
  if (v4) {
    const value = parseV4(v4[2]);
    if (value === null) return null;
    head = v4[1] + ((value >> 16n) & 0xffffn).toString(16) + ":" + (value & 0xffffn).toString(16);
  }
  const halves = head.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const missing = 8 - left.length - right.length;
  if ((halves.length === 1 && missing !== 0) || missing < 0 || (halves.length === 2 && missing < 1)) return null;
  const groups = [...left, ...Array(halves.length === 2 ? missing : 0).fill("0"), ...right];
  if (!groups.every((g) => /^[0-9a-f]{1,4}$/i.test(g))) return null;
  return groups.reduce((acc, g) => (acc << 16n) | BigInt(parseInt(g, 16)), 0n);
}

function parseAddress(host: string): Address | null {
  const v4 = parseV4(host);
  if (v4 !== null) return { version: 4, value: v4 };
  const v6 = host.includes(":") ? parseV6(host) : null;
  return v6 === null ? null : { version: 6, value: v6 };
}

function inNetwork(address: Address, cidr: string): boolean {
  const [base, bits] = cidr.split("/");
  const network = parseAddress(base);
  if (network === null || network.version !== address.version) return false;
  const width = address.version === 4 ? 32n : 128n;
  const shift = width - BigInt(bits);
  return address.value >> shift === network.value >> shift;
}

function ipv4Mapped(address: Address): Address | null {
  return address.version === 6 && address.value >> 32n === 0xffffn
    ? { version: 4, value: address.value & 0xffffffffn } : null;
}

function isPrivate(address: Address): boolean {
  const mapped = ipv4Mapped(address);
  if (mapped) return isPrivate(mapped);
  const [nets, exceptions] = address.version === 4 ? [V4_PRIVATE, V4_EXCEPTIONS] : [V6_PRIVATE, V6_EXCEPTIONS];
  return nets.some((n) => inNetwork(address, n)) && !exceptions.some((n) => inNetwork(address, n));
}

export function isGlobal(address: Address): boolean {
  const mapped = ipv4Mapped(address);
  if (mapped) return isGlobal(mapped);
  return !(address.version === 4 && inNetwork(address, V4_SHARED)) && !isPrivate(address);
}

function isLoopback(address: Address): boolean {
  const mapped = ipv4Mapped(address);
  if (mapped) return isLoopback(mapped);
  return address.version === 4 ? inNetwork(address, "127.0.0.0/8") : address.value === 1n;
}

const AUTHORITY = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/[^/?#]/;

export interface PolicyOptions {
  capabilities?: Iterable<string>;
  effects?: Iterable<Effect>;
  accounts?: Iterable<string>;
  allowLoopback?: boolean;
  allowLocal?: boolean;
}

export class Policy {
  readonly capabilities: ReadonlySet<string>;
  readonly effects: ReadonlySet<string>;
  readonly accounts: ReadonlySet<string>;
  readonly allowLoopback: boolean;
  readonly allowLocal: boolean;
  private readonly approved: ReadonlyMap<string, string>;

  constructor(approved: Iterable<Integration> = [], options: PolicyOptions = {}) {
    this.approved = new Map([...approved].map((i) => [i.id, fingerprint(i)]));
    this.capabilities = new Set(options.capabilities ?? []);
    this.effects = new Set(options.effects ?? ["read", "draft"]);
    this.accounts = new Set(options.accounts ?? ["default"]);
    this.allowLoopback = options.allowLoopback ?? false;
    this.allowLocal = options.allowLocal ?? true;
    Object.freeze(this);
  }

  check(integration: Integration, capability: string | null = null, account = "default"): void {
    if (this.approved.get(integration.id) !== fingerprint(integration)) {
      throw new PolicyDenied("Integration/version/configuration is not approved by the host");
    }
    if (integration.support !== "supported" && integration.support !== "experimental") {
      throw new PolicyDenied("Integration has no supported adapter");
    }
    if (!this.accounts.has(account)) throw new PolicyDenied("Account is outside the host policy");
    if (capability !== null && (!this.capabilities.has(capability) || !integration.capabilities.includes(capability))) {
      throw new PolicyDenied("Capability is outside the host policy");
    }
    if (integration.transport === "stdio") {
      if (!this.allowLocal) throw new PolicyDenied("Local execution is disabled");
      if (!integration.command.length && !integration.package) {
        throw new PolicyDenied("Local integration needs a configured runtime");
      }
    } else {
      this.checkEndpoint(integration.endpoint ?? "");
    }
  }

  checkEndpoint(endpoint: string): void {
    if (!AUTHORITY.test(endpoint) || /[\\\s\x00-\x1f]/.test(endpoint)) {
      throw new PolicyDenied("Endpoint must be an absolute URL without backslashes or whitespace");
    }
    let url: URL;
    try {
      url = new URL(endpoint);
    } catch {
      throw new PolicyDenied("Endpoint host is neither a valid address nor a DNS name");
    }
    if (!url.hostname || url.username || url.password || url.search || url.hash) {
      throw new PolicyDenied("Endpoint must not contain credentials, query parameters or fragments");
    }
    const host = url.hostname.replace(/^\[|\]$/g, "").replace(/\.+$/, "");
    const address = parseAddress(host);
    const loopback = host === "localhost" || host.endsWith(".localhost") || (address !== null && isLoopback(address));
    const scheme = url.protocol.slice(0, -1);
    if (loopback && this.allowLoopback && (scheme === "http" || scheme === "https")) return;
    if (scheme !== "https" || loopback || (address !== null && !isGlobal(address))) {
      throw new PolicyDenied("Remote MCP endpoints require HTTPS and a public address");
    }
  }

  checkTool(integration: Integration, rule: ToolRule, account: string): void {
    this.check(integration, rule.capability, account);
    if (!this.effects.has(rule.effect)) throw new PolicyDenied("Tool effect requires a host policy change");
  }
}
