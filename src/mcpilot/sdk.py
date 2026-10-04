"""Asynchronous host-owned MCP orchestration. No model or agent loop is bundled."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .auth import AuthManager
from .catalog import Catalog
from .errors import (
    BudgetExceeded,
    ConnectionUnavailable,
    InvalidArguments,
    MCPilotError,
    PolicyDenied,
    UncertainOutcome,
)
from .models import (
    AuditEvent,
    CallResult,
    Connection,
    Integration,
    Limits,
    Plan,
    TaskRequest,
    Tool,
    ToolSet,
)
from .policy import Policy
from .router import Reranker, route
from .runtime import Runtime

logger = logging.getLogger(__name__)
AuditSink = Callable[[AuditEvent], Awaitable[None] | None]
_current_task: ContextVar[str | None] = ContextVar("mcpilot_task", default=None)


@dataclass
class _TaskBudget:
    """Step/cost budget of one task. A long-lived session (e.g. a gateway user) runs many tasks."""

    task_id: str
    steps: int = 0
    cost: float = 0.0


def validate_schema(schema: dict) -> None:
    """Schemas are data; reject remote references and excessive payloads before validation."""
    if len(json.dumps(schema).encode()) > 100_000:
        raise ValueError("Tool schema exceeds limit")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("External schema references are disabled")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    Draft202012Validator.check_schema(schema)


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


class MCPilot:
    def __init__(
        self,
        *,
        user_id: str,
        catalog: Catalog,
        policy: Policy,
        auth: AuthManager | None = None,
        runtime: Runtime | None = None,
        state_dir: str | Path = ".mcpilot",
        limits: Limits | None = None,
        reranker: Reranker | None = None,
        token_counter: Callable[[str], int] | None = None,
        audit: AuditSink | None = None,
        tenant: str | None = None,
    ) -> None:
        if not user_id:
            raise ValueError("Host-authenticated user_id is required")
        self.user_id, self.catalog, self.policy = user_id, catalog, policy
        self.auth = auth or AuthManager()
        self.runtime = runtime or Runtime(Path(state_dir) / "runtimes")
        self.limits = limits or Limits()
        self.reranker = reranker
        self.audit = audit
        self.tenant = tenant
        # UTF-8 bytes is a deliberately conservative token upper bound, not len(text)/4.
        self.token_counter = token_counter or (lambda text: len(text.encode("utf-8")))
        self._connections: dict[tuple[str, str], Connection] = {}
        self._sessions: dict[str, Any] = {}
        self._schemas: dict[str, list[dict]] = {}
        self._issued: dict[str, Tool] = {}
        self._budgets: dict[str, _TaskBudget] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._call_lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> MCPilot:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _integration(self, integration_id: str) -> Integration:
        try:
            return self.catalog.get(integration_id)
        except KeyError:
            raise PolicyDenied("Integration is not in the host catalog") from None

    async def discover(self, task: str | TaskRequest, *, limit: int = 5) -> list[dict]:
        """Return small policy-filtered summaries, never the full registry or tool schemas."""
        plan = await self.plan(task)
        result = []
        for selection in plan.selections[:max(0, min(limit, 20))]:
            integration = self._integration(selection.integration_id)
            result.append({"id": integration.id, "service": integration.service,
                           "capability": selection.capability, "account": selection.account,
                           "support": integration.support, "auth": integration.auth.mode})
        return result

    async def plan(self, task: str | TaskRequest) -> Plan:
        request = TaskRequest(task=task) if isinstance(task, str) else task
        return await route(request, self.catalog.all(), self.policy, reranker=self.reranker,
                           connected={key for key, c in self._connections.items() if c.status == "ready"})

    async def _audit(self, action: str, outcome: str, **fields: Any) -> None:
        if self.audit is None:
            return
        fields.setdefault("task_id", _current_task.get())
        event = AuditEvent(at=datetime.now(UTC).isoformat(), user_id=self.user_id, tenant=self.tenant,
                           action=action, outcome=outcome, **fields)
        try:
            pending = self.audit(event)
            if inspect.isawaitable(pending):
                await pending
        except Exception:
            # The action already happened; never turn a completed write into an error.
            logger.warning("MCPilot audit sink failed for action %s", action)

    async def connect(
        self, integration_id: str, *, account: str = "default", capabilities: tuple[str, ...] | None = None,
    ) -> Connection:
        before = self._connections.get((integration_id, account))
        started = time.perf_counter()
        try:
            connection = await self._connect(integration_id, account=account, capabilities=capabilities)
        except MCPilotError as exc:
            await self._audit("connect", type(exc).__name__, integration_id=integration_id, account=account,
                              duration_ms=_elapsed(started))
            raise
        if connection is not before:  # reuse of a live connection is not a new event
            await self._audit("connect", connection.status, integration_id=integration_id, account=account,
                              connection_id=connection.id, duration_ms=_elapsed(started))
        return connection

    async def _connect(
        self, integration_id: str, *, account: str = "default", capabilities: tuple[str, ...] | None = None,
    ) -> Connection:
        if self._closed:
            raise ConnectionUnavailable("SDK is closed")
        integration = self._integration(integration_id)
        caps = tuple(sorted(set(capabilities if capabilities is not None else (
            c for c in integration.capabilities if c in self.policy.capabilities
        ))))
        self.policy.check(integration, account=account)
        if not caps:
            raise PolicyDenied("No capabilities authorized for this connection")
        for cap in caps:
            self.policy.check(integration, cap, account)
        key = (integration_id, account)
        async with self._locks.setdefault(key, asyncio.Lock()):
            previous = self._connections.get(key)
            if previous and previous.status == "ready" and set(caps) <= set(previous.capabilities):
                try:
                    await self._sessions[previous.id].ping()
                    return previous
                except Exception:
                    await self._drop(previous)
            elif previous:
                await self._drop(previous)
            handle = await self.auth.prepare(self.user_id, integration, account, caps)
            connection = Connection(id=handle.connection_id, integration_id=integration_id,
                                    account=account, status=handle.status, capabilities=caps)
            self._connections[key] = connection
            if handle.status != "ready":
                return connection
            session = None
            try:
                if handle.authorize is not None:
                    await handle.authorize()
                session = await self.runtime.open(integration, auth=handle.http_auth,
                                                  headers=handle.headers, env=handle.env)
                schemas = await session.list_tools()
                names = set()
                valid = []
                for schema in schemas:
                    name = schema["name"]
                    if name in names:
                        raise ValueError("Duplicate MCP tool names")
                    names.add(name)
                    rule = integration.tools.get(name)
                    # Unmapped tools and effects outside policy are hidden, not fatal.
                    if not rule or rule.capability not in caps or rule.effect not in self.policy.effects:
                        continue
                    self.policy.check_tool(integration, rule, account)
                    validate_schema(schema["input_schema"])
                    if schema.get("output_schema"):
                        validate_schema(schema["output_schema"])
                    valid.append(schema)
                self._sessions[connection.id], self._schemas[connection.id] = session, valid
                connection = connection.model_copy(update={"status": "ready" if valid else "connected"})
            except Exception as exc:
                if session:
                    await session.close()
                # Auth exceptions may be preserved by the runtime; only safe class/status enter UI.
                status = getattr(exc, "status", "error")
                if status not in {"auth_required", "setup_required"}:
                    status = "error"
                connection = connection.model_copy(update={
                    "status": status,
                    "message": "Connection needs authorization or setup" if status != "error" else
                               "Connection failed; check host configuration and provider availability",
                })
            self._connections[key] = connection
            return connection

    async def tools_for(self, task: str | TaskRequest | Plan, *, token_budget: int | None = None) -> ToolSet:
        plan = task if isinstance(task, Plan) else await self.plan(task)
        groups: dict[tuple[str, str], set[str]] = {}
        for selection in plan.selections:
            groups.setdefault((selection.integration_id, selection.account), set()).add(selection.capability)
        connections, tools = [], []
        truncated = False
        budget = min(token_budget if token_budget is not None else self.limits.tool_tokens,
                     self.limits.tool_tokens)
        used = self.token_counter("[]")
        task = _TaskBudget(task_id=uuid.uuid4().hex)
        marker = _current_task.set(task.task_id)
        try:
            return await self._issue(plan, groups, task, budget, used, truncated, connections, tools)
        finally:
            _current_task.reset(marker)

    async def _issue(self, plan: Plan, groups: dict[tuple[str, str], set[str]], task: _TaskBudget, budget: int,
                     used: int, truncated: bool, connections: list, tools: list) -> ToolSet:
        for (integration_id, account), capabilities in groups.items():
            integration = self._integration(integration_id)
            connection = await self.connect(integration_id, account=account,
                                            capabilities=tuple(sorted(capabilities)))
            connections.append(connection)
            if connection.status != "ready":
                continue
            for schema in sorted(self._schemas[connection.id], key=lambda t: t["name"]):
                rule = integration.tools[schema["name"]]
                if rule.capability not in capabilities:
                    continue
                self.policy.check_tool(integration, rule, account)
                digest = hashlib.sha256(f"{connection.id}:{schema['name']}".encode()).hexdigest()[:32]
                tool = Tool(id=f"mcp_{digest}", connection_id=connection.id,
                            integration_id=integration_id, name=schema["name"],
                            description=(schema.get("description") or "")[:2000],
                            input_schema=schema["input_schema"], output_schema=schema.get("output_schema"),
                            capability=rule.capability, effect=rule.effect)
                cost = self.token_counter(tool.model_dump_json()) + 32
                if used + cost > budget:
                    truncated = True
                    continue
                used += cost
                tools.append(tool)
                self._issued[tool.id] = tool
                self._budgets[tool.id] = task  # the latest task that issued a tool pays for it
        return ToolSet(tools=tuple(tools), connections=tuple(connections), token_estimate=used,
                       truncated=truncated, task_id=task.task_id)

    async def call(self, tool_id: str, arguments: dict[str, Any], *, idempotency_key: str | None = None) -> CallResult:
        tool = self._issued.get(tool_id)
        task = self._budgets.get(tool_id)
        fields = {} if tool is None else {
            "integration_id": tool.integration_id, "connection_id": tool.connection_id,
            "tool": tool.name, "effect": tool.effect, "task_id": task.task_id if task else None,
        }
        started = time.perf_counter()
        try:
            result = await self._call(tool_id, arguments, idempotency_key=idempotency_key)
        except MCPilotError as exc:
            await self._audit("call", type(exc).__name__, duration_ms=_elapsed(started), **fields)
            raise
        await self._audit("call", "tool_error" if result.is_error else "ok", duration_ms=_elapsed(started), **fields)
        return result

    async def _call(self, tool_id: str, arguments: dict[str, Any], *, idempotency_key: str | None) -> CallResult:
        tool = self._issued.get(tool_id)
        if not tool:
            raise PolicyDenied("Tool was not exposed by tools_for in this user session")
        integration = self._integration(tool.integration_id)
        connection = next((c for c in self._connections.values() if c.id == tool.connection_id), None)
        if not connection or connection.status != "ready":
            raise ConnectionUnavailable("Tool connection is not ready")
        rule = integration.tools[tool.name]
        self.policy.check_tool(integration, rule, connection.account)
        payload = dict(arguments)
        if idempotency_key:
            if not rule.idempotency_parameter:
                raise PolicyDenied("Provider adapter does not support idempotency keys")
            payload[rule.idempotency_parameter] = idempotency_key
        try:
            Draft202012Validator(tool.input_schema).validate(payload)
        except Exception:
            raise InvalidArguments("Arguments do not match the discovered MCP tool schema") from None
        task = self._budgets[tool_id]
        attempts = 1 + (self.limits.read_retries if rule.effect == "read" else 0)
        for attempt in range(attempts):
            async with self._call_lock:
                if task.steps >= self.limits.max_steps or task.cost + integration.cost_per_call > self.limits.max_cost:
                    raise BudgetExceeded("Step or estimated cost limit reached for this task; "
                                         "request tools again to start a new task")
                task.steps += 1
                task.cost += integration.cost_per_call
            try:
                raw = await self._sessions[connection.id].call_tool(tool.name, payload)
            except (Exception, asyncio.CancelledError) as exc:
                if rule.effect in {"write", "send"}:
                    raise UncertainOutcome("Mutation outcome is unknown; reconcile before retrying") from None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                raise ConnectionUnavailable("Tool call failed after bounded read retries") from None
            if len(json.dumps(raw, default=str).encode()) > self.limits.max_result_bytes:
                raise BudgetExceeded("Tool result exceeds context limit; narrow the query")
            if tool.output_schema and raw.get("structured_content") is not None and not raw.get("is_error"):
                try:
                    Draft202012Validator(tool.output_schema).validate(raw["structured_content"])
                except Exception:
                    raise ConnectionUnavailable("Tool returned an invalid output schema") from None
            return CallResult(tool_id=tool.id, content=raw.get("content", []),
                              structured_content=raw.get("structured_content"), is_error=raw.get("is_error", False))
        raise AssertionError("unreachable")

    async def diagnose(self, integration_id: str, *, account: str = "default") -> dict[str, Any]:
        """Host-side mapping check: the server's tool names versus the manifest. Never give it to a model.

        A connection whose server renamed its tools reports ``connected`` with no tools; this
        shows which manifest names are missing and which server tools are unmapped.
        """
        connection = self._connections.get((integration_id, account))
        session = self._sessions.get(connection.id) if connection else None
        if session is None:
            raise ConnectionUnavailable("Connect the integration before diagnosing it")
        names = {tool["name"] for tool in await session.list_tools()}
        mapped = set(self._integration(integration_id).tools)
        return {"connection_status": connection.status, "server_tools": sorted(names),
                "mapped_present": sorted(mapped & names), "mapped_missing": sorted(mapped - names),
                "unmapped": sorted(names - mapped)}

    async def status(self, integration_id: str | None = None) -> tuple[Connection, ...]:
        return tuple(c for c in self._connections.values()
                     if integration_id is None or c.integration_id == integration_id)

    async def _drop(self, connection: Connection) -> None:
        session = self._sessions.pop(connection.id, None)
        if session:
            await session.close()
        self._schemas.pop(connection.id, None)
        self._issued = {key: value for key, value in self._issued.items()
                        if value.connection_id != connection.id}
        self._budgets = {key: value for key, value in self._budgets.items() if key in self._issued}

    async def disconnect(self, integration_id: str, *, account: str = "default", revoke: bool = False) -> None:
        connection = self._connections.get((integration_id, account))
        if connection and connection.status != "disconnected":
            await self._drop(connection)
            self._connections[(integration_id, account)] = connection.model_copy(update={"status": "disconnected"})
            await self._audit("disconnect", "ok", integration_id=integration_id, account=account,
                              connection_id=connection.id)
        if revoke:
            integration = self._integration(integration_id)
            try:
                await self.auth.revoke(self.user_id, integration_id, account, endpoint=integration.endpoint)
            except Exception:
                await self._audit("revoke", "failed", integration_id=integration_id, account=account)
                raise
            await self._audit("revoke", "ok", integration_id=integration_id, account=account)

    async def close(self) -> None:
        if self._closed:
            return
        for connection in tuple(self._connections.values()):
            await self.disconnect(connection.integration_id, account=connection.account)
        await self.runtime.close()
        self._closed = True
