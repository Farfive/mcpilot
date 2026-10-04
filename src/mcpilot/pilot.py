"""Pilot protocol runner (docs/acceptance.md): one integration, one account, a redacted report.

    python -m mcpilot.pilot --manifest approved.json --integration com.notion/mcp \\
        --tool notion-search --arguments '{"query": "..."}' --interactive \\
        --report docs/pilots/2026-10-05-notion.md

Steps: login/connect -> mapping check (tools/list) -> read -> restart and reuse without login
-> forced token refresh -> provider revocation (interactive) -> local revocation.
The report holds statuses, tool names, sizes and durations; never tokens, login URLs,
argument values or tool results.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from . import __version__
from .auth import AuthManager, CredentialKey, EncryptedFileSecretStore, OAuthConfig
from .catalog import Catalog
from .gateway import LoopbackLogin
from .models import Integration, TaskRequest
from .policy import Policy, fingerprint
from .sdk import MCPilot

USER = "pilot-user"
REVOKE_HELP = {
    "github": "GitHub → Settings → Developer settings → Personal access tokens: usuń token użyty w przebiegu.",
    "notion": "Notion → Settings → Connections: odłącz połączenie MCP użyte w przebiegu.",
}


@dataclass
class Step:
    name: str
    status: str  # pass | fail | skipped | n/a
    detail: str
    duration_ms: float | None = None


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _evidence(integration: Integration) -> str:
    host = urlsplit(integration.endpoint or "").hostname
    local = integration.transport == "stdio" or host in {"127.0.0.1", "localhost", "::1"}
    return "Demonstracyjne (lokalny fixture)" if local else "Konto dostawcy"


async def run_pilot(
    *,
    integration: Integration,
    tool: str,
    arguments: dict[str, Any],
    state_dir: Path,
    key: bytes,
    secret: str | None = None,
    account: str = "default",
    login_port: int = 8765,
    opener: Callable[[str], Any] = webbrowser.open,
    interactive: bool = False,
    prompt: Callable[[str], Any] = input,
    allow_loopback: bool = False,
    login_timeout: float = 300,
) -> dict[str, Any]:
    rule = integration.tools.get(tool)
    if rule is None or rule.effect != "read":
        raise ValueError("The pilot runs one mapped read tool; writes and sends are out of scope")
    capability = rule.capability
    store = EncryptedFileSecretStore(state_dir / "credentials", key)
    key_record = CredentialKey(USER, integration.id, account, integration.endpoint or "stdio")
    catalog = Catalog([integration])
    policy = Policy([integration], capabilities=[capability], accounts=[account], allow_loopback=allow_loopback)
    request = TaskRequest(task=f"pilot {integration.service}", capabilities=(capability,),
                          services=(integration.service,), accounts={integration.service: account})
    logins: list[str] = []
    steps: list[Step] = []
    login = None
    oauth = integration.auth.mode == "oauth"

    def counting_opener(url: str) -> Any:
        logins.append("login")
        return opener(url)

    def manager(with_login: bool) -> AuthManager:
        if oauth and with_login:
            return AuthManager(store, OAuthConfig(login=login.broker, redirect_uri=login.redirect_uri))
        return AuthManager(store)

    async def session(with_login: bool = False) -> MCPilot:
        return MCPilot(user_id=USER, catalog=catalog, policy=policy, auth=manager(with_login),
                       state_dir=state_dir / "runtime")

    async def connect(with_login: bool = False) -> str:
        async with await session(with_login) as pilot:
            return (await pilot.tools_for(request)).connections[0].status

    if oauth:
        login = LoopbackLogin(USER, port=login_port, timeout=login_timeout, opener=counting_opener)
        login.start()
    elif integration.auth.mode in {"bearer", "api_key"}:
        if not secret:
            raise ValueError("This integration needs a credential (--secret-env)")
        await AuthManager(store).set_secret(USER, integration.id, account, secret,
                                            endpoint=integration.endpoint or "stdio")
    try:
        # 1. Login / connect and 2. mapping check, 3. read — one session.
        started = time.perf_counter()
        async with await session(with_login=True) as pilot:
            toolset = await pilot.tools_for(request)
            status = toolset.connections[0].status if toolset.connections else "missing"
            steps.append(Step("logowanie i połączenie", "pass" if status == "ready" else "fail",
                              f"status={status}; logowań w przeglądarce={len(logins)}", _ms(started)))
            try:
                diag = await pilot.diagnose(integration.id, account=account)
                ok = tool in diag["mapped_present"]
                steps.append(Step("mapowanie narzędzi (tools/list)", "pass" if ok else "fail",
                                  f"narzędzi serwera={len(diag['server_tools'])}; zmapowane obecne={diag['mapped_present']}; "
                                  f"zmapowane brakujące={diag['mapped_missing']}; niezmapowane={len(diag['unmapped'])}"))
                server_tools = diag["server_tools"]
            except Exception as exc:
                steps.append(Step("mapowanie narzędzi (tools/list)", "fail", type(exc).__name__))
                server_tools = []
            target = next((t for t in toolset.tools if t.name == tool), None)
            started = time.perf_counter()
            if target is None:
                steps.append(Step("odczyt", "fail", "narzędzie niedostępne po połączeniu"))
            else:
                try:
                    result = await pilot.call(target.id, arguments)
                    size = len(json.dumps(result.model_dump(mode="json")).encode())
                    steps.append(Step("odczyt", "fail" if result.is_error else "pass",
                                      f"narzędzie={tool}; argumenty={sorted(arguments)}; bloków={len(result.content)}; "
                                      f"bajtów={size}; is_error={result.is_error}", _ms(started)))
                except Exception as exc:
                    steps.append(Step("odczyt", "fail", f"narzędzie={tool}; błąd={type(exc).__name__}", _ms(started)))

        # 4. Restart: new AuthManager on the same encrypted store, no login UI.
        before = len(logins)
        started = time.perf_counter()
        status = await connect()
        steps.append(Step("ponowne użycie po restarcie", "pass" if status == "ready" and len(logins) == before else "fail",
                          f"status={status}; nowe logowania={len(logins) - before}", _ms(started)))

        # 5. Refresh: expire the stored grant and reconnect without a login UI.
        record = await store.get(key_record) or {}
        tokens = record.get("tokens") or {}
        if not oauth:
            steps.append(Step("odświeżenie tokenu", "n/a", f"metoda {integration.auth.mode}: brak tokenu OAuth"))
        elif not tokens.get("refresh_token"):
            steps.append(Step("odświeżenie tokenu", "n/a", "dostawca nie wydał refresh token"))
        else:
            fingerprint_before = hashlib.sha256(str(tokens.get("access_token")).encode()).hexdigest()
            record["expires_at"] = time.time() - 60
            await store.set(key_record, record)
            started = time.perf_counter()
            status = await connect()
            after = (await store.get(key_record) or {}).get("tokens") or {}
            rotated = hashlib.sha256(str(after.get("access_token")).encode()).hexdigest() != fingerprint_before
            steps.append(Step("odświeżenie tokenu", "pass" if status == "ready" and rotated else "fail",
                              f"status={status}; nowy access token={rotated}", _ms(started)))

        # 6. Provider-side revocation needs a person at the provider's settings page.
        if interactive:
            prompt(f"Cofnij dostęp u dostawcy: {REVOKE_HELP.get(integration.service, 'w ustawieniach dostawcy')} "
                   "Następnie naciśnij Enter. ")
            started = time.perf_counter()
            status = await connect()
            steps.append(Step("cofnięcie u dostawcy", "pass" if status != "ready" else "fail",
                              f"status po cofnięciu={status}", _ms(started)))
        else:
            steps.append(Step("cofnięcie u dostawcy", "skipped", "uruchom z --interactive, aby potwierdzić"))

        # 7. Local revocation removes the stored grant; a reconnect must need the user again.
        async with await session() as pilot:
            await pilot.disconnect(integration.id, account=account, revoke=True)
        status = await connect()
        steps.append(Step("cofnięcie lokalne", "pass" if status == "auth_required" else "fail",
                          f"status po cofnięciu={status}; rekord usunięty={await store.get(key_record) is None}"))
    finally:
        if login is not None:
            login.close()

    required = {s.name: s.status for s in steps}
    core = ["logowanie i połączenie", "mapowanie narzędzi (tools/list)", "odczyt", "ponowne użycie po restarcie",
            "cofnięcie lokalne"]
    if any(required[name] != "pass" for name in core) or required["odświeżenie tokenu"] == "fail" \
            or required["cofnięcie u dostawcy"] == "fail":
        verdict = "fail"
    elif required["cofnięcie u dostawcy"] == "skipped":
        verdict = "pass_without_provider_revoke"
    else:
        verdict = "pass"
    return {
        "date": datetime.now(UTC).date().isoformat(),
        "mcpilot_version": __version__,
        "evidence": _evidence(integration),
        "provider": integration.service,
        "integration_id": integration.id,
        "manifest_fingerprint": fingerprint(integration),
        "transport": integration.transport,
        "endpoint": integration.endpoint,
        "auth_mode": integration.auth.mode,
        "requested_scopes": list(integration.auth.scopes_by_capability.get(capability, ())),
        "account": account,
        "capability": capability,
        "server_tools": server_tools,
        "steps": [asdict(step) for step in steps],
        "verdict": verdict,
    }


def render_markdown(report: dict[str, Any]) -> str:
    rows = "\n".join(f"| {s['name']} | {s['status']} | {s['detail']} | {s['duration_ms'] if s['duration_ms'] is not None else ''} |"
                     for s in report["steps"])
    tools = ", ".join(f"`{name}`" for name in report["server_tools"]) or "—"
    return f"""# Przebieg pilotażowy: {report['provider']} ({report['date']})

| Pole | Wartość |
| --- | --- |
| Status dowodu | {report['evidence']} |
| Werdykt | **{report['verdict']}** |
| Integracja | `{report['integration_id']}` (odcisk manifestu `{report['manifest_fingerprint'][:16]}…`) |
| Transport / endpoint | {report['transport']} / {report['endpoint'] or 'lokalny proces'} |
| Metoda autoryzacji | {report['auth_mode']}; scope: {', '.join(report['requested_scopes']) or 'brak'} |
| Konto (etykieta) | {report['account']} |
| Capability | {report['capability']} |
| MCPilot | {report['mcpilot_version']} |

| Krok protokołu | Wynik | Szczegóły (bez treści i sekretów) | Czas [ms] |
| --- | --- | --- | --- |
{rows}

Narzędzia serwera (`tools/list`): {tools}

Raport nie zawiera tokenów, adresów logowania, wartości argumentów ani wyników narzędzi.
"""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mcpilot.pilot", description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", required=True, help="host-approved manifest (list or {'integrations': [...]})")
    parser.add_argument("--integration", required=True)
    parser.add_argument("--tool", required=True, help="mapped read tool to call")
    parser.add_argument("--arguments", default="{}", help="JSON arguments for the read tool")
    parser.add_argument("--account", default="default")
    parser.add_argument("--state-dir", default=".mcpilot/pilot")
    parser.add_argument("--key-env", default="MCPILOT_SECRET_KEY", help="Fernet key; ephemeral if unset")
    parser.add_argument("--secret-env", help="environment variable with a PAT/API key (bearer, api_key)")
    parser.add_argument("--login-port", type=int, default=8765)
    parser.add_argument("--allow-loopback", action="store_true", help="development endpoints on 127.0.0.1")
    parser.add_argument("--interactive", action="store_true", help="pause to revoke access at the provider")
    parser.add_argument("--report", help="Markdown report path")
    parser.add_argument("--json", help="JSON report path")
    args = parser.parse_args(argv)
    catalog = Catalog()
    catalog.load_manifest(args.manifest)
    integration = catalog.get(args.integration)
    key = os.environ.get(args.key_env, "").encode() or Fernet.generate_key()
    secret = os.environ.get(args.secret_env) if args.secret_env else None
    report = asyncio.run(run_pilot(
        integration=integration, tool=args.tool, arguments=json.loads(args.arguments), state_dir=Path(args.state_dir),
        key=key, secret=secret, account=args.account, login_port=args.login_port, interactive=args.interactive,
        allow_loopback=args.allow_loopback))
    markdown = render_markdown(report)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(markdown, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(markdown)
    sys.exit(0 if report["verdict"] != "fail" else 1)


if __name__ == "__main__":
    main()
