"""Durable, sequential host-created workflows with explicit uncertain outcomes."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .errors import MCPilotError, UncertainOutcome
from .models import CallResult, Model, Plan, Selection
from .sdk import MCPilot


class WorkflowStep(Model):
    integration_id: str
    capability: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    account: str = "default"
    idempotency_key: str | None = None


class StepState(Model):
    step: WorkflowStep
    status: Literal["pending", "running", "complete", "uncertain", "failed"] = "pending"
    result: CallResult | None = None


class WorkflowRun(Model):
    id: str
    user_id: str
    task: str
    steps: tuple[StepState, ...]
    status: Literal["pending", "running", "waiting_for_auth", "complete", "uncertain", "failed"] = "pending"
    message: str | None = None
    attempted_steps: int = 0
    estimated_cost: float = 0


class WorkflowStore:
    """Local host-owned data, including tool results; use protected storage in production."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, body TEXT NOT NULL)")

    def save(self, run: WorkflowRun) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body WHERE runs.user_id=excluded.user_id",
                       (run.id, run.user_id, run.model_dump_json()))

    def load(self, run_id: str, user_id: str) -> WorkflowRun:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT body FROM runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
        if row is None:
            raise MCPilotError("Workflow is not available to this user")
        return WorkflowRun.model_validate_json(row[0])

    @contextmanager
    def execution_lock(self, run_id: str):
        digest = hashlib.sha256(run_id.encode()).hexdigest()
        lock_path = self.path.parent / f".workflow-{digest}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MCPilotError("Workflow is already executing") from None
            yield
        finally:
            os.close(fd)


class WorkflowRunner:
    def __init__(self, pilot: MCPilot, store: WorkflowStore) -> None:
        self.pilot, self.store = pilot, store

    async def create(self, steps: list[WorkflowStep], *, task: str) -> str:
        for step in steps:
            integration = self.pilot.catalog.get(step.integration_id)
            rule = integration.tools.get(step.tool_name)
            if rule is None or rule.capability != step.capability:
                raise MCPilotError("Workflow tool is not mapped to its requested capability")
            self.pilot.policy.check_tool(integration, rule, step.account)
        run = WorkflowRun(id=uuid.uuid4().hex, user_id=self.pilot.user_id, task=task,
                          steps=tuple(StepState(step=s) for s in steps))
        self.store.save(run)
        return run.id

    async def resume(self, run_id: str) -> WorkflowRun:
        with self.store.execution_lock(run_id):
            return await self._resume(run_id)

    async def _resume(self, run_id: str) -> WorkflowRun:
        run = self.store.load(run_id, self.pilot.user_id)
        if run.status in {"complete", "uncertain", "failed"}:
            return run
        states = list(run.steps)

        def save(**changes: Any) -> WorkflowRun:
            nonlocal run
            run = run.model_copy(update={"steps": tuple(states), **changes})
            self.store.save(run)
            return run

        for index, state in enumerate(states):
            if state.status == "complete":
                continue
            step = state.step
            integration = self.pilot.catalog.get(step.integration_id)
            rule = integration.tools[step.tool_name]
            self.pilot.policy.check_tool(integration, rule, step.account)
            if state.status == "running" and rule.effect != "read":
                states[index] = state.model_copy(update={"status": "uncertain"})
                return save(status="uncertain", message="Interrupted mutation requires provider reconciliation")
            plan = Plan(task=run.task, selections=(Selection(
                integration_id=step.integration_id, capability=step.capability,
                account=step.account, score=0, reason="Host-created persisted workflow",
            ),))
            toolset = await self.pilot.tools_for(plan)
            if any(c.status in {"auth_required", "setup_required"} for c in toolset.connections):
                return save(status="waiting_for_auth", message="Connect the required account in the host UI, then resume")
            tool = next((t for t in toolset.tools if t.name == step.tool_name), None)
            if tool is None:
                return save(status="failed", message="Required tool is unavailable or exceeds context budget")
            # Reserve worst-case read retry cost durably before starting network I/O.
            attempts = 1 + (self.pilot.limits.read_retries if rule.effect == "read" else 0)
            cost = integration.cost_per_call * attempts
            if run.attempted_steps + attempts > self.pilot.limits.max_steps or run.estimated_cost + cost > self.pilot.limits.max_cost:
                return save(status="failed", message="Persisted workflow step or estimated cost limit reached")
            states[index] = state.model_copy(update={"status": "running"})
            save(status="running", attempted_steps=run.attempted_steps + attempts,
                 estimated_cost=run.estimated_cost + cost, message=None)
            try:
                result = await self.pilot.call(tool.id, step.arguments, idempotency_key=step.idempotency_key)
            except (Exception, asyncio.CancelledError) as exc:
                uncertain = isinstance(exc, UncertainOutcome) or rule.effect != "read"
                states[index] = state.model_copy(update={"status": "uncertain" if uncertain else "failed"})
                save(status="uncertain" if uncertain else "failed",
                     message="Provider reconciliation required" if uncertain else "Step failed; inspect host configuration")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return run
            states[index] = state.model_copy(update={"status": "failed" if result.is_error else "complete", "result": result})
            save(status="failed" if result.is_error else "running")
            if result.is_error:
                return run
        return save(status="complete", message=None)

    async def reconcile(self, run_id: str, step_index: int, result: CallResult) -> WorkflowRun:
        """Host-only: record a result confirmed with the provider; never blindly replay a write."""
        with self.store.execution_lock(run_id):
            run = self.store.load(run_id, self.pilot.user_id)
            states = list(run.steps)
            if states[step_index].status != "uncertain":
                raise MCPilotError("Only an uncertain step can be reconciled")
            states[step_index] = states[step_index].model_copy(update={"status": "complete", "result": result})
            run = run.model_copy(update={"status": "pending", "steps": tuple(states), "message": None})
            self.store.save(run)
            return run
