"""Export cross-language test vectors from the Python implementation.

Run: python scripts/export_vectors.py   (writes spec/vectors.json)
The TypeScript SDK tests consume the file; tests/test_vectors.py fails when it is stale.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcpilot.canonical import canonical_json, sha256_hex
from mcpilot.catalog import _summary
from mcpilot.discovery import DEFINITIONS
from mcpilot.errors import PolicyDenied
from mcpilot.integrations import github, notion, provider_candidates
from mcpilot.models import (
    AuditEvent,
    AuthSpec,
    CallResult,
    Integration,
    PackageSpec,
    TaskRequest,
    ToolRule,
)
from mcpilot.policy import Policy, fingerprint
from mcpilot.router import requirements, route
from mcpilot.workflow import StepState, WorkflowRun, WorkflowStep

TARGET = Path(__file__).resolve().parent.parent / "spec" / "vectors.json"
FLAGSHIP = "Znajdź dokumentację projektu w Notion, porównaj ją z issue w GitHub i przygotuj podsumowanie"


def dump(model: Any) -> Any:
    return model.model_dump(mode="json")


def local_server(**changes: Any) -> Integration:
    values: dict[str, Any] = dict(
        id="example/calculator", service="calculator", publisher="Example", version="1.0.0",
        transport="stdio", command=("python", "-m", "calculator"), support="supported",
        capabilities=("math.add", "counter.write"),
        tools={"add": ToolRule(capability="math.add"),
               "write": ToolRule(capability="counter.write", effect="write", idempotency_parameter="request_id")},
        quality=0.7, cost_per_call=0.25, latency_ms=350,
    )
    values.update(changes)
    return Integration(**values)


def integrations() -> list[Integration]:
    drive, slack = provider_candidates()[2:]
    return [
        github(), notion(), drive, slack, local_server(),
        local_server(id="example/calculator-b", quality=0.7, cost_per_call=0.25, latency_ms=350),
        Integration(id="io.modelcontextprotocol/filesystem", service="filesystem", publisher="MCP",
                    version="2026.8.31", transport="stdio", support="experimental",
                    package=PackageSpec(runtime="node", name="@modelcontextprotocol/server-filesystem",
                                        version="2026.8.31", entrypoint="mcp-server-filesystem", args=("/srv/docs",)),
                    capabilities=("files.read",), tools={"read_text_file": ToolRule(capability="files.read")},
                    auth=AuthSpec(mode="api_key", env_var="FS_KEY", scopes_by_capability={"files.read": ("read",)})),
        local_server(id="example/odd-numbers", quality=0.1, cost_per_call=0.00001, latency_ms=1e-7),
        local_server(id="example/unicode", service="calculator", publisher="Zażółć gęślą jaźń 😀",
                     tools={"dodaj ": ToolRule(capability="math.add")}),
    ]


def policy_spec(**values: Any) -> dict[str, Any]:
    base = {"capabilities": [], "effects": ["read", "draft"], "accounts": ["default"],
            "allow_loopback": False, "allow_local": True}
    base.update(values)
    return base


def build_policy(approved: list[Integration], spec: dict[str, Any]) -> Policy:
    return Policy(approved, capabilities=spec["capabilities"], effects=spec["effects"], accounts=spec["accounts"],
                  allow_loopback=spec["allow_loopback"], allow_local=spec["allow_local"])


async def routes() -> list[dict[str, Any]]:
    gh, nt, drive, slack, calc, calc_b, fs, odd, uni = integrations()
    cases = [
        ("flagship", [gh, nt, drive, slack], [gh, nt, drive, slack],
         policy_spec(capabilities=["issues.read", "documents.search"], accounts=["default", "work"]),
         TaskRequest(task=FLAGSHIP, accounts={"github": "work"}), []),
        ("setup_required", [drive, slack], [drive, slack], policy_spec(capabilities=["documents.search"]),
         TaskRequest(task="Znajdź dokument w Google Drive"), []),
        ("score_and_reuse", [calc, calc_b, odd], [calc, calc_b, odd], policy_spec(capabilities=["math.add"]),
         TaskRequest(task="add", capabilities=("math.add",)), [["example/calculator-b", "default"]]),
        ("changed_manifest_denied", [calc.model_copy(update={"version": "1.0.1"})], [calc],
         policy_spec(capabilities=["math.add"]), TaskRequest(task="add", capabilities=("math.add",)), []),
        ("named_service_read_only", [calc, uni], [calc, uni],
         policy_spec(capabilities=["math.add", "counter.write"], effects=["read", "write"]),
         TaskRequest(task="policz coś", services=("calculator",)), []),
        ("no_tool_mapping", [calc], [calc], policy_spec(capabilities=["counter.write"]),
         TaskRequest(task="zapisz", capabilities=("counter.write",)), []),
        ("account_outside_policy", [gh], [gh], policy_spec(capabilities=["issues.read"]),
         TaskRequest(task="Read GitHub issues", accounts={"github": "personal"}), []),
        ("local_disabled", [fs], [fs], policy_spec(capabilities=["files.read"], allow_local=False),
         TaskRequest(task="Przeczytaj lokalny plik README"), []),
    ]
    vectors = []
    for name, catalog, approved, spec, request, connected in cases:
        plan = await route(request, catalog, build_policy(approved, spec),
                           connected={tuple(item) for item in connected})
        vectors.append({"name": name, "catalog": [dump(i) for i in catalog],
                        "approved": [dump(i) for i in approved], "policy": spec, "request": dump(request),
                        "connected": connected, "plan": dump(plan)})
    return vectors


def endpoint_vectors() -> list[dict[str, Any]]:
    cases = [
        ("https://api.example.com/mcp", False), ("http://api.example.com/mcp", False),
        ("https://10.0.0.1/mcp", False), ("https://127.0.0.1/mcp", False), ("http://127.0.0.1:8000/mcp", True),
        ("http://localhost:9/mcp", True), ("http://localhost:9/mcp", False), ("https://[::1]/mcp", True),
        ("https://user:pw@api.example.com/mcp", False), ("https://api.example.com/mcp?token=1", False),
        ("https://api.example.com/mcp#x", False), ("https://8.8.8.8/mcp", False), ("ftp://api.example.com", False),
        ("https://169.254.169.254/latest", False), ("https://[2001:4860:4860::8888]/mcp", False),
        ("https://100.64.0.1/mcp", False), ("https://192.0.0.9/mcp", False), ("https://192.0.0.8/mcp", False),
        ("https://224.0.0.1/mcp", False), ("https://240.0.0.1/mcp", False), ("https://172.31.255.255/mcp", False),
        ("https://172.32.0.1/mcp", False), ("https://[2001:db8::1]/mcp", False), ("https://[fe80::1]/mcp", False),
        ("https://[fc00::1]/mcp", False), ("https://[::ffff:10.0.0.1]/mcp", False),
        ("https://[::ffff:8.8.8.8]/mcp", False), ("https://[2001:3::1]/mcp", False), ("https://[2002::1]/mcp", False),
        ("https://[::]/mcp", False), ("https://0.0.0.0/mcp", False), ("https://LOCALHOST:9/mcp", True),
        ("HTTPS://API.EXAMPLE.COM/mcp", False), ("https://api.example.com:8443/mcp", False),
        ("https://localhost./mcp", False), ("https://api.localhost/mcp", False), ("http://api.localhost:9/mcp", True),
        ("https://127.1/mcp", False), ("https://0x7f.0.0.1/mcp", False), ("https://2130706433/mcp", False),
        ("https://example.123/mcp", False), ("https://8.8.8.8./mcp", False), ("https:///mcp", False),
        ("https:/api.example.com/mcp", False), ("https://evil.example\\@good.example/mcp", False),
        ("https://api.example.com/m cp", False), ("https://api.example.com./mcp", False),
    ]
    vectors = []
    for endpoint, loopback in cases:
        try:
            Policy(allow_loopback=loopback).check_endpoint(endpoint)
            allowed = True
        except PolicyDenied:
            allowed = False
        vectors.append({"endpoint": endpoint, "allow_loopback": loopback, "allowed": allowed})
    return vectors


def registry_vectors() -> list[dict[str, Any]]:
    meta = "io.modelcontextprotocol.registry/official"
    entries = [
        {"server": {"name": "io.example/server", "version": "1.2.3", "description": "Szukaj\u0000 dokumentów\n\t w  Notion " + "x" * 600,
                    "remotes": [{"type": "streamable-http", "url": "https://evil.example/mcp"}, {"type": "sse"}],
                    "packages": [{"identifier": "evil", "transport": {"type": "stdio"}}]},
         "_meta": {meta: {"status": "active", "updatedAt": "2026-10-04T02:29:51.000704Z"}}},
        {"server": {"name": "io.example/deleted", "version": "0.1.0"}, "_meta": {meta: {"status": "deleted"}}},
        {"server": {"name": "io.example/plain", "version": "2.0.0", "description": 5}},
        {"server": {"name": "", "version": "1"}},
        {"server": {"name": "io.example/x", "version": "1"}, "_meta": {meta: {"status": "unknown"}}},
        {"server": {"name": "io.example/x", "version": "1"}, "_meta": []},
        {"server": "not-an-object"},
    ]
    vectors = []
    for entry in entries:
        try:
            vectors.append({"entry": entry, "summary": _summary(entry)})
        except ValueError:
            vectors.append({"entry": entry, "error": True})
    return vectors


def requirement_vectors() -> list[dict[str, Any]]:
    requests = [
        TaskRequest(task=FLAGSHIP), TaskRequest(task="Przygotuj wiadomość na Slack"),
        TaskRequest(task="Wyślij podsumowanie na Slack"), TaskRequest(task="ąsend a note to slack"),
        TaskRequest(task="Read GitHub issues", accounts={"github": "work"}),
        TaskRequest(task="Przeczytaj lokalny plik README"), TaskRequest(task="Find documents about pricing"),
        TaskRequest(task="Coś zupełnie innego"), TaskRequest(task="policz", services=("calculator",)),
        TaskRequest(task="Sprawdź dysk Google", capabilities=("documents.search", "files.read")),
        TaskRequest(task="Zgłoszenia i błędy w repozytorium"), TaskRequest(task="Wiadomości z czatu"),
    ]
    return [{"request": dump(r), "expected": [dump(x) for x in requirements(r)]} for r in requests]


def credential_vectors() -> list[dict[str, Any]]:
    from mcpilot.auth import CredentialKey

    vectors = []
    for user, integration, account, endpoint in [
        ("user-123", "com.notion/mcp", "default", "https://mcp.notion.com/mcp"),
        ("ąę|😀", "com.github/remote", "work", "https://api.githubcopilot.com/mcp/readonly"),
        ("https://idp.example.com|alice", "example/calculator", "default", "stdio"),
    ]:
        key = CredentialKey(user, integration, account, endpoint)
        connection_id = f"conn_{key.digest[:24]}"
        vectors.append({"user_id": user, "integration_id": integration, "account": account, "endpoint": endpoint,
                        "digest": key.digest, "connection_id": connection_id,
                        "tool_ids": {name: "mcp_" + sha256_hex(f"{connection_id}:{name}")[:32]
                                     for name in ("notion-search", "read_file", "dodaj")}})
    return vectors


def workflow_vector() -> dict[str, Any]:
    run = WorkflowRun(id="0" * 32, user_id="user-123", task=FLAGSHIP, status="waiting_for_auth",
                      message="Connect the required account in the host UI, then resume",
                      attempted_steps=4, estimated_cost=0.5, steps=(
                          StepState(step=WorkflowStep(integration_id="com.github/remote", capability="issues.read",
                                                      tool_name="issue_read", arguments={"issue_number": 42}),
                                    status="complete", result=CallResult(tool_id="mcp_" + "a" * 32,
                                                                         content=[{"type": "text", "text": "#42"}])),
                          StepState(step=WorkflowStep(integration_id="com.notion/mcp", capability="documents.search",
                                                      tool_name="notion-search", arguments={"query": "Atlas"}))))
    return dump(run)


def fernet_vector() -> dict[str, Any]:
    """Deterministic token (fixed time and IV) proving the TypeScript store reads Python credentials."""
    import base64

    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    plaintext = json.dumps({"tokens": {"access_token": "fixture-access-token", "token_type": "Bearer"}})
    token = Fernet(key)._encrypt_from_parts(plaintext.encode(), 1_759_600_000, bytes(range(100, 116))).decode()
    return {"key": key, "plaintext": plaintext, "time": 1_759_600_000, "iv": list(range(100, 116)), "token": token}


async def build() -> dict[str, Any]:
    canonical_inputs = [
        None, True, False, 0, -0.0, 1, -1, 2**53 - 1, 0.1, 1e-05, 1.5e-07, 100.0, 1e21, 1e16, 4.35, 5e-324,
        123456789012345680000.0, "", "ą\u0007\"\\\n\r\t\b\f€😀 \u007f",
        {"b": [1, {"d": None, "c": "x"}], "a": True, "€": 1, "\U0001F600": 2, "｡": 3, "A": 0, "_": 0},
    ]
    invalid = [
        {"id": "x", "service": "s", "publisher": "p", "version": "1", "transport": "http"},
        {"id": "x", "service": "s", "publisher": "p", "version": "1", "transport": "stdio", "endpoint": "https://a"},
        {"id": "x", "service": "s", "publisher": "p", "version": "1", "transport": "http", "endpoint": "https://a",
         "command": ["sh"]},
        {"id": "x", "service": "s", "publisher": "p", "version": "1", "transport": "stdio", "command": ["sh"],
         "unexpected": True},
        {"id": "x", "service": "s", "publisher": "p", "version": "1", "transport": "stdio", "quality": 2},
        {"id": "x", "service": "s", "publisher": "p", "version": "1", "transport": "stdio",
         "tools": {"t": {"capability": "c", "effect": "delete"}}},
    ]
    for item in invalid:
        try:
            Integration.model_validate(item)
            raise SystemExit(f"Expected invalid manifest: {item}")
        except ValueError:
            pass
    minimal = {"id": "example/min", "service": "min", "publisher": "Example", "version": "1", "transport": "http",
               "endpoint": "https://min.example.com/mcp"}
    return {
        "version": 1,
        "generator": "scripts/export_vectors.py",
        "canonical": [{"value": value, "json": canonical_json(value)} for value in canonical_inputs],
        "integrations": [{"input": dump(i), "normalized": dump(i), "fingerprint": fingerprint(i)}
                         for i in integrations()]
                        + [{"input": minimal, "normalized": dump(Integration.model_validate(minimal)),
                            "fingerprint": fingerprint(Integration.model_validate(minimal))}],
        "invalid_integrations": invalid,
        "endpoints": endpoint_vectors(),
        "credentials": credential_vectors(),
        "requirements": requirement_vectors(),
        "routes": await routes(),
        "registry": registry_vectors(),
        "discovery_definitions": list(DEFINITIONS),
        "workflow_run": workflow_vector(),
        "fernet": fernet_vector(),
        "audit_event_fields": list(AuditEvent.model_fields),
    }


def render() -> str:
    return json.dumps(asyncio.run(build()), ensure_ascii=False, indent=1, sort_keys=False) + "\n"


if __name__ == "__main__":
    TARGET.parent.mkdir(exist_ok=True)
    TARGET.write_text(render(), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(Path.cwd()) if TARGET.is_relative_to(Path.cwd()) else TARGET}", file=sys.stderr)
