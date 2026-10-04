/**
 * Durable, sequential host-created workflows with explicit uncertain outcomes.
 * The persisted JSON matches the Python `WorkflowRun` model.
 */

import { MCPilotError, UncertainOutcome } from "./errors.js";
import type { CallResult } from "./models.js";
import type { MCPilot } from "./sdk.js";

export interface WorkflowStep {
  integration_id: string;
  capability: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  account: string;
  idempotency_key: string | null;
}

export interface StepState {
  step: WorkflowStep;
  status: "pending" | "running" | "complete" | "uncertain" | "failed";
  result: CallResult | null;
}

export interface WorkflowRun {
  id: string;
  user_id: string;
  task: string;
  steps: StepState[];
  status: "pending" | "running" | "waiting_for_auth" | "complete" | "uncertain" | "failed";
  message: string | null;
  attempted_steps: number;
  estimated_cost: number;
}

/** Persistence for runs. Implementations must scope `load` to the owning user. */
export interface WorkflowStore {
  save(run: WorkflowRun): Promise<void>;
  load(runId: string, userId: string): Promise<WorkflowRun>;
  /** Exclusive execution; resolves to a release function or throws if already held. */
  lock(runId: string): Promise<() => Promise<void>>;
}

export class MemoryWorkflowStore implements WorkflowStore {
  private readonly runs = new Map<string, WorkflowRun>();
  private readonly held = new Set<string>();

  async save(run: WorkflowRun) {
    const existing = this.runs.get(run.id);
    if (existing && existing.user_id !== run.user_id) return;
    this.runs.set(run.id, structuredClone(run));
  }

  async load(runId: string, userId: string) {
    const run = this.runs.get(runId);
    if (!run || run.user_id !== userId) throw new MCPilotError("Workflow is not available to this user");
    return structuredClone(run);
  }

  async lock(runId: string) {
    if (this.held.has(runId)) throw new MCPilotError("Workflow is already executing");
    this.held.add(runId);
    return async () => { this.held.delete(runId); };
  }
}

export function parseWorkflowRun(value: unknown): WorkflowRun {
  const run = value as WorkflowRun;
  const statuses = ["pending", "running", "waiting_for_auth", "complete", "uncertain", "failed"];
  if (!run || typeof run.id !== "string" || typeof run.user_id !== "string" || !Array.isArray(run.steps)
      || !statuses.includes(run.status)) {
    throw new TypeError("Invalid workflow run");
  }
  return structuredClone(run);
}

type StepInput = Omit<WorkflowStep, "account" | "idempotency_key" | "arguments">
  & Partial<Pick<WorkflowStep, "account" | "idempotency_key" | "arguments">>;

export class WorkflowRunner {
  constructor(private readonly pilot: MCPilot, private readonly store: WorkflowStore) {}

  async create(steps: StepInput[], { task }: { task: string }): Promise<string> {
    const normalized: WorkflowStep[] = steps.map((s) => ({ arguments: {}, account: "default", idempotency_key: null, ...s }));
    for (const step of normalized) {
      const integration = this.pilot.catalog.get(step.integration_id);
      const rule = integration.tools[step.tool_name];
      if (!rule || rule.capability !== step.capability) {
        throw new MCPilotError("Workflow tool is not mapped to its requested capability");
      }
      this.pilot.policy.checkTool(integration, rule, step.account);
    }
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
    await this.store.save({ id, user_id: this.pilot.userId, task, status: "pending", message: null, attempted_steps: 0,
                            estimated_cost: 0, steps: normalized.map((step) => ({ step, status: "pending", result: null })) });
    return id;
  }

  async resume(runId: string): Promise<WorkflowRun> {
    const release = await this.store.lock(runId);
    try {
      return await this.run(runId);
    } finally {
      await release();
    }
  }

  private async run(runId: string): Promise<WorkflowRun> {
    let run = await this.store.load(runId, this.pilot.userId);
    if (run.status === "complete" || run.status === "uncertain" || run.status === "failed") return run;
    const save = async (changes: Partial<WorkflowRun>) => {
      run = { ...run, ...changes, steps: [...run.steps] };
      await this.store.save(run);
      return run;
    };
    for (let index = 0; index < run.steps.length; index++) {
      const state = run.steps[index];
      if (state.status === "complete") continue;
      const { step } = state;
      const integration = this.pilot.catalog.get(step.integration_id);
      const rule = integration.tools[step.tool_name];
      this.pilot.policy.checkTool(integration, rule, step.account);
      if (state.status === "running" && rule.effect !== "read") {
        run.steps[index] = { ...state, status: "uncertain" };
        return save({ status: "uncertain", message: "Interrupted mutation requires provider reconciliation" });
      }
      const toolset = await this.pilot.toolsFor({ task: run.task, missing: [], unavailable: [], selections: [{
        integration_id: step.integration_id, capability: step.capability, account: step.account, score: 0,
        reason: "Host-created persisted workflow" }] });
      if (toolset.connections.some((c) => c.status === "auth_required" || c.status === "setup_required")) {
        return save({ status: "waiting_for_auth", message: "Connect the required account in the host UI, then resume" });
      }
      const tool = toolset.tools.find((t) => t.name === step.tool_name);
      if (!tool) return save({ status: "failed", message: "Required tool is unavailable or exceeds context budget" });
      // Reserve worst-case read retry cost durably before starting network I/O.
      const attempts = 1 + (rule.effect === "read" ? this.pilot.limits.read_retries : 0);
      const cost = integration.cost_per_call * attempts;
      if (run.attempted_steps + attempts > this.pilot.limits.max_steps || run.estimated_cost + cost > this.pilot.limits.max_cost) {
        return save({ status: "failed", message: "Persisted workflow step or estimated cost limit reached" });
      }
      run.steps[index] = { ...state, status: "running" };
      await save({ status: "running", attempted_steps: run.attempted_steps + attempts,
                   estimated_cost: run.estimated_cost + cost, message: null });
      let result: CallResult;
      try {
        result = await this.pilot.call(tool.id, step.arguments, step.idempotency_key ? { idempotencyKey: step.idempotency_key } : {});
      } catch (error) {
        const uncertain = error instanceof UncertainOutcome || rule.effect !== "read";
        run.steps[index] = { ...state, status: uncertain ? "uncertain" : "failed" };
        return save({ status: uncertain ? "uncertain" : "failed",
                      message: uncertain ? "Provider reconciliation required" : "Step failed; inspect host configuration" });
      }
      run.steps[index] = { ...state, status: result.is_error ? "failed" : "complete", result };
      await save({ status: result.is_error ? "failed" : "running" });
      if (result.is_error) return run;
    }
    return save({ status: "complete", message: null });
  }

  /** Host-only: record a result confirmed with the provider; never blindly replay a write. */
  async reconcile(runId: string, stepIndex: number, result: CallResult): Promise<WorkflowRun> {
    const release = await this.store.lock(runId);
    try {
      const run = await this.store.load(runId, this.pilot.userId);
      if (run.steps[stepIndex]?.status !== "uncertain") throw new MCPilotError("Only an uncertain step can be reconciled");
      run.steps[stepIndex] = { ...run.steps[stepIndex], status: "complete", result };
      const updated = { ...run, status: "pending" as const, message: null };
      await this.store.save(updated);
      return updated;
    } finally {
      await release();
    }
  }
}
