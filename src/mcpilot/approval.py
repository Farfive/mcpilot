"""Human approval of an MCP Registry server: registry entry → pinned, read-only manifest.

    python -m mcpilot approve com.atlassian/atlassian-mcp-server

Registry data stays untrusted. Approval is a person at a terminal: it reads the
entry, connects once (browser login when the server asks for it), shows the
server's tools and writes a manifest only after an explicit "yes". Defaults:

* only tools the server annotates ``readOnlyHint`` are mapped (effect ``read``);
  other tools need ``--include-write`` and a second confirmation. The annotation
  is a hint from the server, so the person reviews the list before approving it;
* the registry version is recorded and the manifest fingerprint pins the exact
  endpoint, package, version and tool mapping — any change needs a new approval;
* remote Streamable HTTP servers use MCP OAuth (login only if the server asks);
* local packages run code on this computer and need ``--local`` (npm) or
  ``--container`` (Docker), plus a typed confirmation before anything runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2 as httpx

from .auth import AuthManager, CredentialKey, OAuthConfig, SecretStore
from .catalog import OFFICIAL_REGISTRY
from .models import AuthSpec, Integration, PackageSpec, ToolRule
from .policy import Policy, fingerprint
from .runtime import Runtime
from .search import _official, _vendor

NPM_REGISTRY = "https://registry.npmjs.org"
_MAX_ENTRY_BYTES = 1_000_000
_NAME = re.compile(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+\Z")
_GENERIC = {"mcp", "server", "remote", "official", "api", "tools", "model", "context", "protocol"}
# Container images for npm/PyPI packages; tags, not digests (see docs/approval.md).
NODE_IMAGE = "node:22-alpine"
UV_IMAGE = "ghcr.io/astral-sh/uv:python3.12-bookworm-slim"


class ApprovalError(ValueError):
    """The entry cannot become a manifest automatically; the message says why (no secrets)."""


@dataclass
class Draft:
    """An unapproved manifest: no tool mapping yet, plus what the person must know."""

    integration: Integration
    kind: str  # "remote" | "local" | "container"
    registry_version: str
    official: bool
    description: str
    warnings: list[str] = field(default_factory=list)
    secret_label: str | None = None  # what to ask for when auth is a token or key


def service_name(server_name: str) -> str:
    """'com.atlassian/atlassian-mcp-server' → 'atlassian'; 'io.github.jdoe/jira-mcp' → 'jira'."""
    rest = server_name.partition("/")[2]
    words = [w for w in re.split(r"[-_.]+", rest.lower()) if w and w not in _GENERIC]
    if _official(server_name):
        candidate = _vendor(server_name)
    else:
        candidate = words[0] if words else _vendor(server_name)
    return re.sub(r"[^a-z0-9-]", "", candidate.lower()) or "server"


async def fetch_entry(name: str, *, version: str = "latest", source: str = OFFICIAL_REGISTRY,
                      client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Full registry entry (list summaries drop remotes and packages)."""
    if not _NAME.fullmatch(name):
        raise ApprovalError("Registry server name must look like 'com.vendor/server'")
    url = f"{source}/{quote(name, safe='')}/versions/{quote(version, safe='')}"
    owned = client is None
    client = client or httpx.AsyncClient(trust_env=False)
    try:
        response = await client.get(url, timeout=20, follow_redirects=False)
        if response.status_code == 404:
            raise ApprovalError(f"Registry has no server '{name}'")
        response.raise_for_status()
        if len(response.content) > _MAX_ENTRY_BYTES:
            raise ApprovalError("Registry entry exceeds size limit")
        data = response.json()
    finally:
        if owned:
            await client.aclose()
    if not isinstance(data, dict) or not isinstance(data.get("server"), dict):
        raise ApprovalError("Invalid registry entry")
    return data


async def npm_entrypoint(name: str, version: str, *, client: httpx.AsyncClient | None = None) -> str:
    """The package's executable name from npm metadata (the registry entry does not carry it)."""
    owned = client is None
    client = client or httpx.AsyncClient(trust_env=False)
    try:
        response = await client.get(f"{NPM_REGISTRY}/{quote(name, safe='@')}/{quote(version, safe='')}",
                                    timeout=20, follow_redirects=False)
        response.raise_for_status()
        binary = response.json().get("bin")
    finally:
        if owned:
            await client.aclose()
    if isinstance(binary, str):
        return name.rpartition("/")[2]
    if isinstance(binary, dict) and len(binary) == 1:
        return next(iter(binary))
    if isinstance(binary, dict) and binary:
        base = name.rpartition("/")[2]
        if base in binary:
            return base
    raise ApprovalError("The npm package does not declare a single executable")


def _status(entry: dict[str, Any]) -> str:
    meta = (entry.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
    return str(meta.get("status") or entry["server"].get("status") or "active")


def _secret_inputs(items: list[dict[str, Any]], what: str) -> tuple[dict[str, Any] | None, list[str]]:
    """One secret input is supported; required non-secret inputs without a value are not."""
    secrets = [i for i in items if i.get("isSecret")]
    missing = [str(i.get("name")) for i in items
               if not i.get("isSecret") and i.get("isRequired") and not (i.get("value") or i.get("default"))]
    problems = [f"required {what} without a value: {', '.join(missing)}"] if missing else []
    if len([s for s in secrets if s.get("isRequired")]) > 1:
        problems.append(f"more than one required secret {what}")
    required = [s for s in secrets if s.get("isRequired")]
    return (required[0] if required else (secrets[0] if secrets else None)), problems


def _docker_prefix() -> tuple[str, ...]:
    docker = shutil.which("docker")
    if docker is None:
        raise ApprovalError("Container mode needs Docker ('docker' not found)")
    return (docker, "run", "-i", "--rm", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "1g", "--tmpfs", "/tmp")


def draft_remote(entry: dict[str, Any], *, auth: str | None = None) -> Draft:
    server = entry["server"]
    name, version = server["name"], str(server.get("version", ""))
    remotes = [r for r in server.get("remotes") or [] if r.get("type") == "streamable-http"]
    if not remotes:
        raise ApprovalError("No Streamable HTTP endpoint in the registry entry")
    remote = remotes[0]
    url = str(remote.get("url", ""))
    if "{" in url:
        raise ApprovalError("The endpoint URL has template variables; write this manifest by hand")
    secret, problems = _secret_inputs(list(remote.get("headers") or []), "header")
    if problems:
        raise ApprovalError("; ".join(problems))
    service = service_name(name)
    warnings: list[str] = []
    label = None
    mode = auth or ("token" if secret and secret.get("isRequired") else "oauth")
    if mode == "token":
        if secret is None:
            raise ApprovalError("The server declares no token header; use OAuth")
        header = str(secret["name"])
        spec = AuthSpec(mode="bearer" if header.lower() == "authorization" else "api_key", header_name=header)
        label = f"{header} ({secret.get('description') or 'token'})"
    else:
        spec = AuthSpec(mode="oauth")
        if secret is not None:
            warnings.append(f"The server also accepts a token in '{secret['name']}' (use --auth token).")
    if len(remotes) > 1:
        warnings.append(f"The registry lists {len(remotes)} endpoints; using the first one.")
    integration = Integration(
        id=name, service=service, publisher=_vendor(name), version=version, transport="http", endpoint=url,
        auth=spec, support="experimental", source=f"registry:{name}@{version}")
    return Draft(integration, "remote", version, _official(name), str(server.get("description", ""))[:300],
                 warnings, label)


async def draft_package(entry: dict[str, Any], *, container: bool,
                        client: httpx.AsyncClient | None = None) -> Draft:
    server = entry["server"]
    name, version = server["name"], str(server.get("version", ""))
    packages = [p for p in server.get("packages") or []
                if (p.get("transport") or {}).get("type", "stdio") == "stdio"
                and p.get("registryType") in {"npm", "pypi", "oci"}]
    if not packages:
        raise ApprovalError("No stdio package (npm, PyPI or OCI) in the registry entry")
    order = ["npm", "pypi", "oci"] if not container else ["oci", "npm", "pypi"]
    package = sorted(packages, key=lambda p: order.index(p["registryType"]))[0]
    kind, identifier = package["registryType"], str(package["identifier"])
    package_version = str(package.get("version") or "")
    secret, problems = _secret_inputs(list(package.get("environmentVariables") or []), "environment variable")
    arguments = list(package.get("packageArguments") or [])
    if any(a.get("isRequired") and not (a.get("value") or a.get("default")) for a in arguments):
        problems.append("required package arguments without a value")
    if problems:
        raise ApprovalError("; ".join(problems))
    args = tuple(str(a.get("value") or a.get("default")) for a in arguments
                 if a.get("type", "positional") == "positional" and (a.get("value") or a.get("default")))
    env_var = str(secret["name"]) if secret else None
    spec = AuthSpec(mode="api_key", env_var=env_var) if env_var else AuthSpec()
    common = dict(id=name, service=service_name(name), publisher=_vendor(name), version=version,
                  transport="stdio", auth=spec, support="experimental", source=f"registry:{name}@{version}")
    warnings = ["This runs code on your computer."]
    if kind == "oci" and not container:
        raise ApprovalError("This package is a container image; use --container")
    if container:
        prefix = _docker_prefix() + (("-e", env_var) if env_var else ())
        if kind == "oci":
            if ":" not in identifier.rpartition("/")[2] and "@sha256:" not in identifier:
                raise ApprovalError("The container image has no pinned tag or digest")
            command = (*prefix, identifier, *args)
        elif kind == "npm":
            command = (*prefix, NODE_IMAGE, "npx", "--yes", f"{identifier}@{package_version}", *args)
        else:
            command = (*prefix, UV_IMAGE, "uvx", f"{identifier}=={package_version}", *args)
        warnings = ["This runs code in a Docker container on your computer (network allowed, "
                    "no host files, no capabilities)."]
        integration = Integration(**common, command=command)
        return Draft(integration, "container", version, _official(name), str(server.get("description", ""))[:300],
                     warnings, f"{env_var} (environment variable)" if env_var else None)
    if kind != "npm":
        raise ApprovalError("PyPI packages do not declare their executable; use --container")
    entrypoint = await npm_entrypoint(identifier, package_version, client=client)
    integration = Integration(**common, package=PackageSpec(runtime="node", name=identifier, version=package_version,
                                                            entrypoint=entrypoint, args=args))
    return Draft(integration, "local", version, _official(name), str(server.get("description", ""))[:300],
                 warnings, f"{env_var} (environment variable)" if env_var else None)


def classify(tools: list[dict[str, Any]], service: str, *, include_write: bool) -> tuple[dict[str, ToolRule], list[str]]:
    """Map server tools to rules: readOnlyHint → read; everything else only when included."""
    rules: dict[str, ToolRule] = {}
    excluded: list[str] = []
    for tool in tools:
        annotations = tool.get("annotations") or {}
        if annotations.get("read_only_hint") is True:
            rules[tool["name"]] = ToolRule(capability=f"{service}.read", effect="read")
        elif include_write:
            rules[tool["name"]] = ToolRule(capability=f"{service}.write", effect="write")
        else:
            excluded.append(tool["name"])
    return rules, excluded


async def list_tools(integration: Integration, auth: AuthManager, *, user_id: str, account: str,
                     state_dir: Path, allow_loopback: bool = False) -> list[dict[str, Any]]:
    """Connect once (browser login if the server asks) and read ``tools/list``."""
    if integration.endpoint:
        Policy(allow_loopback=allow_loopback).check_endpoint(integration.endpoint)
    handle = await auth.prepare(user_id, integration, account, ())
    if handle.status != "ready":
        raise ApprovalError(f"Cannot connect: {handle.reason or handle.status}")
    if handle.authorize is not None:
        await handle.authorize()
    async with Runtime(state_dir / "runtime") as runtime:
        session = await runtime.open(integration, auth=handle.http_auth, headers=handle.headers, env=handle.env)
        try:
            return await session.list_tools()
        finally:
            await session.close()


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(draft: Draft, status: str) -> list[str]:
    i = draft.integration
    where = i.endpoint if i.endpoint else (
        f"npm {i.package.name}@{i.package.version}" if i.package else " ".join(i.command[-3:]))
    lines = [
        f"Serwer:     {i.id} (wersja {draft.registry_version}, status w rejestrze: {status})",
        f"Wydawca:    {i.publisher} — {'oficjalna przestrzeń nazw dostawcy' if draft.official else 'NIE oficjalny dostawca (konto prywatne)'}",
        f"Opis:       {_short(draft.description, 120)}",
        f"Usługa:     {i.service}",
        f"Uruchomienie: {draft.kind} — {where}",
        f"Logowanie:  {({'oauth': 'OAuth w przeglądarce (jeśli serwer wymaga)', 'bearer': 'token', 'api_key': 'klucz API', 'none': 'brak'})[i.auth.mode]}",
    ]
    lines += [f"Uwaga:      {w}" for w in draft.warnings]
    return lines


def tool_lines(tools: list[dict[str, Any]], rules: dict[str, ToolRule], excluded: list[str]) -> list[str]:
    descriptions = {t["name"]: t.get("description", "") for t in tools}
    lines = [f"  [odczyt]  {n:<32} {_short(descriptions[n], 70)}" for n, r in rules.items() if r.effect == "read"]
    lines += [f"  [ZAPIS]   {n:<32} {_short(descriptions[n], 70)}" for n, r in rules.items() if r.effect != "read"]
    lines += [f"  [pominięte, zapis] {n}" for n in excluded]
    return lines


def _write_json(path: Path, value: Any) -> None:
    """Atomic replace so a running gateway never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
    os.replace(temporary, path)


def save(config_path: Path, integration: Integration) -> dict[str, Any]:
    """Add/replace the manifest entry and allow its capabilities (and write effect) in gateway.json."""
    settings = json.loads(config_path.read_text(encoding="utf-8"))
    manifest_path = (config_path.parent / Path(settings["manifest"]).expanduser()).resolve()
    raw = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    items = raw["integrations"] if isinstance(raw, dict) else raw
    items = [item for item in items if item.get("id") != integration.id] + [integration.model_dump(mode="json")]
    _write_json(manifest_path, {"integrations": items} if isinstance(raw, dict) or not raw else items)
    capabilities = sorted({rule.capability for rule in integration.tools.values()})
    settings["capabilities"] = list(dict.fromkeys([*settings.get("capabilities", []), *capabilities]))
    if any(rule.effect == "write" for rule in integration.tools.values()):
        settings["effects"] = list(dict.fromkeys([*settings.get("effects", ["read", "draft"]), "write"]))
    _write_json(config_path, settings)
    record = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "action": "approve", "id": integration.id,
              "version": integration.version, "fingerprint": fingerprint(integration),
              "tools": sorted(integration.tools), "capabilities": capabilities}
    with (config_path.parent / "approvals.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    return record


def remove(config_path: Path, integration_id: str) -> bool:
    """Withdraw an approval; its capabilities stay in gateway.json but match no manifest."""
    settings = json.loads(config_path.read_text(encoding="utf-8"))
    manifest_path = (config_path.parent / Path(settings["manifest"]).expanduser()).resolve()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = raw["integrations"] if isinstance(raw, dict) else raw
    kept = [item for item in items if item.get("id") != integration_id]
    if len(kept) == len(items):
        return False
    _write_json(manifest_path, {"integrations": kept} if isinstance(raw, dict) else kept)
    with (config_path.parent / "approvals.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), "action": "remove",
                              "id": integration_id}) + "\n")
    return True


def _yes(answer: str) -> bool:
    return answer.strip().lower() in {"t", "tak", "y", "yes"}


async def approve(
    name: str, *, config_path: Path, store: SecretStore, user_id: str, ask: Callable[[str], str],
    say: Callable[[str], None], secret_prompt: Callable[[str], str] | None = None,
    login: OAuthConfig | None = None, auth: str | None = None, local: bool = False, container: bool = False,
    include_write: bool = False, account: str = "default", state_dir: Path, allow_loopback: bool = False,
    source: str = OFFICIAL_REGISTRY, client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Interactive approval; returns the approval record, or None when the person declines."""
    entry = await fetch_entry(name, source=source, client=client)
    status = _status(entry)
    if status == "deleted":
        raise ApprovalError("The registry marks this server as deleted")
    remote = any(r.get("type") == "streamable-http" for r in entry["server"].get("remotes") or [])
    if local or container or not remote:
        if not (local or container):
            raise ApprovalError("This server has no remote endpoint; it runs locally. "
                                "Use --local (npm on this computer) or --container (Docker).")
        draft = await draft_package(entry, container=container, client=client)
    else:
        draft = draft_remote(entry, auth=auth)
    if status == "deprecated":
        draft.warnings.append("The registry marks this server as deprecated.")
    for line in describe(draft, status):
        say(line)
    if draft.kind != "remote":
        say("")
        say("!!! To uruchomi kod na Twoim komputerze" + (" (w kontenerze Docker)." if draft.kind == "container" else
            " — bez piaskownicy systemowej, z dostępem do sieci i Twoich plików."))
        if ask("Wpisz 'uruchom', aby kontynuować: ").strip().lower() != "uruchom":
            say("Przerwano. Nic nie zostało uruchomione ani zapisane.")
            return None
    elif not _yes(ask("Połączyć się, aby pobrać listę narzędzi? [t/N] ")):
        say("Przerwano. Nic nie zostało zapisane.")
        return None
    integration = draft.integration
    manager = AuthManager(store, login)
    if integration.auth.mode in {"bearer", "api_key"}:
        if secret_prompt is None:
            raise ApprovalError("This server needs a token; run approve in a terminal")
        secret = secret_prompt(f"{draft.secret_label} (niewidoczne): ").strip()
        if not secret:
            say("Przerwano: brak tokenu.")
            return None
        await manager.set_secret(user_id, integration.id, account, secret,
                                 endpoint=integration.endpoint or "stdio")
    tools = await list_tools(integration, manager, user_id=user_id, account=account, state_dir=state_dir,
                             allow_loopback=allow_loopback)
    if integration.auth.mode == "oauth" and not (await store.get(CredentialKey(
            user_id, integration.id, account, integration.endpoint or "stdio")) or {}).get("tokens"):
        # The server answered without asking for a login: record it as public.
        integration = integration.model_copy(update={"auth": AuthSpec()})
        say("Logowanie:  serwer nie wymaga logowania (publiczny)")
    rules, excluded = classify(tools, integration.service, include_write=include_write)
    say("")
    say(f"Narzędzia serwera ({len(tools)}):")
    for line in tool_lines(tools, rules, excluded):
        say(line)
    if not rules:
        say("Serwer nie oznacza żadnego narzędzia jako tylko do odczytu (readOnlyHint). "
            "Nic nie zapisano; --include-write dopuszcza zapis po osobnym potwierdzeniu.")
        return None
    reads = sum(r.effect == "read" for r in rules.values())
    writes = len(rules) - reads
    say("")
    say("Adnotacje narzędzi pochodzą od serwera — sprawdź listę przed zatwierdzeniem.")
    if not _yes(ask(f"Zatwierdzić {reads} narzędzi do odczytu{f' i {writes} do ZAPISU' if writes else ''}? [t/N] ")):
        say("Przerwano. Nic nie zostało zapisane.")
        return None
    if writes and ask("Zapis zmienia dane w usłudze. Wpisz 'zapis', aby potwierdzić: ").strip().lower() != "zapis":
        say("Przerwano. Nic nie zostało zapisane.")
        return None
    approved = integration.model_copy(update={"tools": rules, "capabilities": tuple(sorted(
        {r.capability for r in rules.values()}))})
    approved = Integration.model_validate(approved.model_dump())
    return await asyncio.to_thread(save, config_path, approved)
