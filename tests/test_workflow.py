from __future__ import annotations

import pytest
from test_sdk import calculator, pilot_for

from mcpilot.errors import MCPilotError, PolicyDenied
from mcpilot.models import CallResult, Limits
from mcpilot.workflow import WorkflowRunner, WorkflowStep, WorkflowStore

ADD = WorkflowStep(integration_id="test/calculator", capability="math.add", tool_name="add",
                   arguments={"left": 20, "right": 22})
WRITE = WorkflowStep(integration_id="test/calculator", capability="counter.write", tool_name="slow_write",
                     arguments={"delay": 0})


async def test_create_validates_mapping_and_policy(tmp_path):
    store = WorkflowStore(tmp_path / "runs.sqlite")
    async with pilot_for(tmp_path, calculator(), capabilities=("math.add", "counter.write")) as pilot:
        runner = WorkflowRunner(pilot, store)
        with pytest.raises(MCPilotError, match="not mapped"):
            await runner.create([ADD.model_copy(update={"tool_name": "process_info"})], task="t")
        with pytest.raises(MCPilotError, match="not mapped"):
            await runner.create([ADD.model_copy(update={"capability": "counter.write"})], task="t")
        with pytest.raises(PolicyDenied):
            await runner.create([WRITE], task="write without write policy")


async def test_resume_completes_persists_and_isolates_users(tmp_path):
    store = WorkflowStore(tmp_path / "runs.sqlite")
    assert oct((tmp_path / "runs.sqlite").stat().st_mode & 0o777) == "0o600"
    async with pilot_for(tmp_path, calculator(), capabilities=("math.add",)) as pilot:
        runner = WorkflowRunner(pilot, store)
        run_id = await runner.create([ADD, ADD], task="add twice")
        run = await runner.resume(run_id)
        assert run.status == "complete" and run.attempted_steps == 4  # two reads, each with retry reserve
        assert [s.result.structured_content for s in run.steps] == [{"sum": 42}, {"sum": 42}]
        assert (await runner.resume(run_id)).status == "complete"
    async with pilot_for(tmp_path, calculator(), capabilities=("math.add",), user="mallory") as other:
        with pytest.raises(MCPilotError, match="not available"):
            await WorkflowRunner(other, store).resume(run_id)


async def test_interrupted_mutation_needs_reconciliation_and_is_not_replayed(tmp_path):
    store = WorkflowStore(tmp_path / "runs.sqlite")
    async with pilot_for(tmp_path, calculator(), capabilities=("math.add", "counter.write"),
                         effects=("read", "write")) as pilot:
        runner = WorkflowRunner(pilot, store)
        run_id = await runner.create([WRITE, ADD], task="write then add")
        crashed = store.load(run_id, "alice")
        steps = list(crashed.steps)
        steps[0] = steps[0].model_copy(update={"status": "running"})  # process died mid-call
        store.save(crashed.model_copy(update={"steps": tuple(steps), "status": "running"}))

        run = await runner.resume(run_id)
        assert run.status == "uncertain" and run.steps[0].status == "uncertain"
        assert (await runner.resume(run_id)).status == "uncertain"  # never retried blindly
        with pytest.raises(MCPilotError, match="Only an uncertain"):
            await runner.reconcile(run_id, 1, CallResult(tool_id="x"))

        confirmed = CallResult(tool_id="provider-confirmed", structured_content={"calls": 1})
        assert (await runner.reconcile(run_id, 0, confirmed)).status == "pending"
        run = await runner.resume(run_id)
        assert run.status == "complete"
        assert run.steps[0].result.tool_id == "provider-confirmed"
        assert run.steps[1].result.structured_content == {"sum": 42}


async def test_concurrent_resume_is_rejected_and_step_limit_is_durable(tmp_path):
    store = WorkflowStore(tmp_path / "runs.sqlite")
    async with pilot_for(tmp_path, calculator(), capabilities=("math.add",)) as pilot:
        runner = WorkflowRunner(pilot, store)
        run_id = await runner.create([ADD], task="locked")
        with store.execution_lock(run_id):
            with pytest.raises(MCPilotError, match="already executing"):
                await runner.resume(run_id)
        assert (await runner.resume(run_id)).status == "complete"

    async with pilot_for(tmp_path, calculator(), capabilities=("math.add",),
                         limits=Limits(max_steps=3)) as pilot:
        runner = WorkflowRunner(pilot, store)
        run = await runner.resume(await runner.create([ADD, ADD], task="over budget"))
        assert run.status == "failed" and "limit" in run.message
        assert [s.status for s in run.steps] == ["complete", "pending"]
