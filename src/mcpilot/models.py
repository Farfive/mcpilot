"""Public, secret-free contracts. Importing this module performs no I/O."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackageSpec(Model):
    runtime: Literal["python", "node"]
    name: str
    version: str
    entrypoint: str | None = None
    args: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


class AuthSpec(Model):
    mode: Literal["none", "oauth", "bearer", "api_key"] = "none"
    scopes_by_capability: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    setup_required: str | None = None
    header_name: str = "Authorization"
    env_var: str | None = None
    target_service_auth: str = "Managed by the MCP server; independent of MCP client authorization."


class ToolRule(Model):
    """Trusted host mapping; server annotations cannot grant permissions."""

    capability: str
    effect: Literal["read", "draft", "write", "send"] = "read"
    idempotency_parameter: str | None = None


class Integration(Model):
    id: str
    service: str
    publisher: str
    version: str
    transport: Literal["http", "stdio"]
    endpoint: str | None = None
    command: tuple[str, ...] = ()
    package: PackageSpec | None = None
    capabilities: tuple[str, ...] = ()
    tools: dict[str, ToolRule] = Field(default_factory=dict)
    auth: AuthSpec = Field(default_factory=AuthSpec)
    support: Literal["discovered", "supported", "experimental", "unavailable"] = "discovered"
    source: str = "host"
    quality: float = Field(default=0.5, ge=0, le=1)
    cost_per_call: float = Field(default=0, ge=0, allow_inf_nan=False)
    latency_ms: float = Field(default=100, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def coherent_transport(self) -> Integration:
        if self.transport == "http" and (not self.endpoint or self.command or self.package):
            raise ValueError("HTTP integrations need an endpoint and no local command/package")
        if self.transport == "stdio" and self.endpoint:
            raise ValueError("stdio integrations cannot have an endpoint")
        if self.transport == "stdio" and self.command and self.package:
            raise ValueError("Choose either a command or a package")
        return self


class TaskRequest(Model):
    task: str
    capabilities: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    accounts: dict[str, str] = Field(default_factory=dict)


class Requirement(Model):
    capability: str
    service: str | None = None
    account: str = "default"


class Selection(Model):
    integration_id: str
    capability: str
    account: str = "default"
    score: float
    reason: str


class Unavailable(Model):
    """Why a known integration cannot serve a requirement; codes only, safe for a model."""

    integration_id: str
    capability: str
    reason: Literal["setup_required", "unsupported", "policy_denied", "no_tool_mapping"]


class Plan(Model):
    task: str
    selections: tuple[Selection, ...] = ()
    missing: tuple[Requirement, ...] = ()
    unavailable: tuple[Unavailable, ...] = ()


class Connection(Model):
    id: str
    integration_id: str
    account: str
    status: Literal["discovered", "supported", "approved", "auth_required", "setup_required", "connected", "ready", "disconnected", "error"]
    capabilities: tuple[str, ...] = ()
    message: str | None = None


class Tool(Model):
    id: str
    connection_id: str
    integration_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    capability: str
    effect: Literal["read", "draft", "write", "send"]
    untrusted: Literal[True] = True


class ToolSet(Model):
    tools: tuple[Tool, ...] = ()
    connections: tuple[Connection, ...] = ()
    token_estimate: int = 0
    truncated: bool = False
    # Calls on these tools share one step/cost budget (Limits); a new tools_for starts a new task.
    task_id: str | None = None


class CallResult(Model):
    tool_id: str
    content: list[dict[str, Any]] = Field(default_factory=list)
    structured_content: Any = None
    is_error: bool = False
    untrusted: Literal[True] = True


class AuditEvent(Model):
    """Secret-free record: no arguments, results, URLs or credentials."""

    at: str
    user_id: str
    action: Literal["connect", "call", "disconnect", "revoke"]
    outcome: str
    integration_id: str | None = None
    account: str | None = None
    connection_id: str | None = None
    tool: str | None = None
    effect: str | None = None
    tenant: str | None = None
    task_id: str | None = None
    duration_ms: float | None = None


class Limits(Model):
    """``max_steps``/``max_cost`` bound one task: a tools_for call and the calls on its tools."""

    max_steps: int = Field(default=20, ge=1)
    max_cost: float = Field(default=1, ge=0, allow_inf_nan=False)
    tool_tokens: int = Field(default=4000, ge=1)
    max_result_bytes: int = Field(default=100_000, ge=1)
    read_retries: int = Field(default=1, ge=0, le=3)
