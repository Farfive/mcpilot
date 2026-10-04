/** Deterministic bilingual baseline plus a constrained optional reranker (parity with Python). */

import { canonicalJson } from "./canonical.js";
import { InvalidProposal, PolicyDenied } from "./errors.js";
import type { Integration, Plan, Requirement, Selection, TaskRequest, Unavailable } from "./models.js";
import type { Policy } from "./policy.js";

export interface RerankCandidate {
  id: string;
  service: string;
  capabilities: string[];
  score: number;
}

/** Proposes an integration id; code accepts only ids from the given candidates. */
export type Reranker = (requirement: Requirement, candidates: RerankCandidate[]) => Promise<string>;

export const SERVICE_ALIASES: Readonly<Record<string, readonly string[]>> = {
  github: ["github"],
  notion: ["notion"],
  "google-drive": ["google drive", "gdrive", "dysk google"],
  slack: ["slack"],
  filesystem: ["filesystem", "local file", "lokaln", "plik lokal", "readme"],
};

export const DEFAULT_CAPABILITIES: Readonly<Record<string, string>> = {
  github: "issues.read",
  notion: "documents.search",
  "google-drive": "documents.search",
  slack: "messages.search",
  filesystem: "files.read",
};

// Unicode word boundaries, matching Python's `\b` on str patterns.
const word = (alternatives: string) =>
  new RegExp(`(?<![\\p{L}\\p{N}_])(?:${alternatives})(?![\\p{L}\\p{N}_])`, "u");
const SEND = word("send|post|wyślij|wyslij|opublikuj");
const DRAFT = word("draft|prepare|przygotuj|szkic");

/** Python-compatible string order (code points, not UTF-16 units). */
export function compareCodePoints(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let i = 0; i < Math.min(left.length, right.length); i++) {
    const diff = left[i].codePointAt(0)! - right[i].codePointAt(0)!;
    if (diff) return diff;
  }
  return left.length - right.length;
}

const unique = <T>(items: T[]) => [...new Set(items)];

export function requirements(request: TaskRequest): Requirement[] {
  const text = request.task.toLowerCase();
  let services = unique([
    ...request.services,
    ...Object.entries(SERVICE_ALIASES).filter(([, aliases]) => aliases.some((a) => text.includes(a))).map(([s]) => s),
  ]);
  // A structured account binding must not be dropped when the text omits a service name.
  services = unique([...services, ...Object.keys(request.accounts)]);
  if (services.length) {
    const found: Requirement[] = [];
    for (const service of services) {
      let caps = request.capabilities.length ? request.capabilities : [DEFAULT_CAPABILITIES[service] ?? "unknown"];
      if (service === "slack" && !request.capabilities.length) {
        if (SEND.test(text)) caps = ["messages.send"];
        else if (DRAFT.test(text)) caps = ["messages.draft"];
      }
      for (const capability of caps) {
        found.push({ capability, service, account: request.accounts[service] ?? "default" });
      }
    }
    return found;
  }
  let caps = request.capabilities;
  if (!caps.length) {
    const has = (...words: string[]) => words.some((w) => text.includes(w));
    if (has("issue", "bug", "zgłosze")) caps = ["issues.read"];
    else if (has("file", "plik")) caps = ["files.read"];
    else if (has("message", "wiadomo", "chat")) caps = ["messages.search"];
    else if (has("document", "dokument", "search", "znajd")) caps = ["documents.search"];
    else caps = ["unknown"];
  }
  return caps.map((capability) => ({ capability, service: null, account: "default" }));
}

function expand(requirement: Requirement, integrations: Integration[], policy: Policy): Requirement[] {
  if (requirement.capability !== "unknown" || !requirement.service) return [requirement];
  const capabilities = unique(integrations
    .filter((i) => i.service === requirement.service)
    .flatMap((i) => Object.values(i.tools))
    .filter((rule) => rule.effect === "read" && policy.capabilities.has(rule.capability))
    .map((rule) => rule.capability)).sort(compareCodePoints);
  return capabilities.length ? capabilities.map((capability) => ({ ...requirement, capability })) : [requirement];
}

function unavailableReason(integration: Integration, requirement: Requirement, policy: Policy): Unavailable["reason"] | null {
  if (integration.support !== "supported" && integration.support !== "experimental") {
    return integration.auth.setup_required ? "setup_required" : "unsupported";
  }
  try {
    policy.check(integration, requirement.capability, requirement.account);
  } catch (error) {
    if (error instanceof PolicyDenied) return "policy_denied";
    throw error;
  }
  const mapped = Object.values(integration.tools)
    .some((rule) => rule.capability === requirement.capability && policy.effects.has(rule.effect));
  return mapped ? null : "no_tool_mapping";
}

export interface RouteOptions {
  connected?: Iterable<[string, string]>;
  reranker?: Reranker;
}

export async function route(
  request: TaskRequest, integrations: Iterable<Integration>, policy: Policy, options: RouteOptions = {},
): Promise<Plan> {
  const available = [...integrations];
  const connected = new Set([...(options.connected ?? [])].map(([id, account]) => canonicalJson([id, account])));
  const selected: Selection[] = [];
  const missing: Requirement[] = [];
  const unavailable: Unavailable[] = [];
  const seen = new Set<string>();
  for (const requirement of requirements(request).flatMap((r) => expand(r, available, policy))) {
    const key = canonicalJson(requirement);
    if (seen.has(key)) continue;
    seen.add(key);
    const candidates: [number, Integration][] = [];
    const rejected: Unavailable[] = [];
    for (const integration of available) {
      if (requirement.service && requirement.service !== integration.service) continue;
      if (!requirement.service && !integration.capabilities.includes(requirement.capability)) continue;
      const reason = unavailableReason(integration, requirement, policy);
      if (reason !== null) {
        rejected.push({ integration_id: integration.id, capability: requirement.capability, reason });
        continue;
      }
      const reused = connected.has(canonicalJson([integration.id, requirement.account])) ? 1 : 0;
      let score = 100 + 20 * reused + 10 * integration.quality;
      score -= Math.min(integration.cost_per_call * 10, 20) + Math.min(integration.latency_ms / 1000, 10);
      candidates.push([score, integration]);
    }
    candidates.sort((a, b) => b[0] - a[0] || compareCodePoints(a[1].id, b[1].id));
    if (!candidates.length) {
      missing.push(requirement);
      unavailable.push(...rejected.sort((a, b) => compareCodePoints(a.integration_id, b.integration_id)).slice(0, 5));
      continue;
    }
    let [score, chosen] = candidates[0];
    if (options.reranker) {
      const top = candidates.slice(0, 10);
      const proposal = await options.reranker(requirement, top.map(([s, i]) => ({
        id: i.id, service: i.service, capabilities: [...i.capabilities], score: s,
      })));
      const match = top.find(([, i]) => i.id === proposal);
      if (!match) throw new InvalidProposal("Reranker proposed an integration outside eligible candidates");
      [score, chosen] = match;
    }
    selected.push({ integration_id: chosen.id, capability: requirement.capability, account: requirement.account,
                    score, reason: "Matches capability, service, account and host policy" });
  }
  return { task: request.task, selections: selected, missing, unavailable };
}
