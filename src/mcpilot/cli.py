"""Pilot kit commands for the local gateway: setup, status, disconnect, uninstall.

    python -m mcpilot setup --workspace ~/docs          # Notion + GitHub (+ local files), Claude Code
    python -m mcpilot approve com.atlassian/atlassian-mcp-server   # any MCP Registry server
    python -m mcpilot remove com.atlassian/atlassian-mcp-server
    python -m mcpilot status
    python -m mcpilot disconnect notion
    python -m mcpilot uninstall

Secrets never go to host configuration (e.g. ~/.claude.json), command-line arguments
or output: the encryption key lives in the OS keychain, provider tokens in the
encrypted store, and a GitHub PAT is read from an environment variable or a hidden prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .approval import ApprovalError
from .approval import approve as approve_entry
from .approval import remove as remove_entry
from .auth import AuthManager, CredentialKey, OAuthConfig
from .catalog import OFFICIAL_REGISTRY, Catalog
from .gateway import GatewayConfig, LoopbackLogin, open_store
from .integrations import filesystem, github, notion
from .keys import KeychainUnavailable, keychain_delete, keychain_ensure, resolve_key
from .pilot import REVOKE_HELP

DEFAULT_DIR = Path("~/.mcpilot/pilot")
SETUP_FILE = "setup.json"  # registration details for uninstall; no secrets


def _config_path(directory: Path) -> Path:
    return directory / "gateway.json"


def _claude(*args: str) -> subprocess.CompletedProcess[str] | None:
    executable = shutil.which("claude")
    if executable is None:
        return None
    return subprocess.run([executable, *args], capture_output=True, text=True, timeout=60, check=False)


def setup(args: argparse.Namespace) -> int:
    directory = Path(args.dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    integrations, capabilities = [], []
    if not args.no_notion:
        integrations.append(notion(args.notion_endpoint) if args.notion_endpoint else notion())
        capabilities.append("documents.search")
    if not args.no_github:
        integrations.append(github(args.github_endpoint) if args.github_endpoint else github())
        capabilities.append("issues.read")
    if args.workspace:
        integrations.append(filesystem(Path(args.workspace).expanduser()))
        capabilities.append("files.read")
    if not integrations:
        print("Nie wybrano żadnej integracji.", file=sys.stderr)
        return 2
    manifest = directory / "approved-integrations.json"
    manifest.write_text(json.dumps({"integrations": [i.model_dump(mode="json") for i in integrations]}, indent=2))
    item = args.keychain_item or "gateway-" + hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    settings: dict[str, Any] = {
        "user_id": args.user, "manifest": manifest.name, "capabilities": capabilities, "effects": ["read", "draft"],
        "state_dir": "state", "keychain_item": item, "audit_log": "audit.jsonl",
        # Registry entries become 'needs_admin_approval' suggestions; never executable.
        "registry_sync": not args.no_registry_sync,
    }
    if args.allow_loopback:
        settings["allow_loopback"] = True
    config_path = _config_path(directory)
    config_path.write_text(json.dumps(settings, indent=2))
    try:
        created = keychain_ensure(item)
    except KeychainUnavailable as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1
    print(f"Konfiguracja: {config_path}")
    print(f"Klucz szyfrujący: pęk kluczy systemu (mcpilot/{item}) — {'utworzony' if created else 'istniał'}")

    if not args.no_github:
        pat = os.environ.get(args.github_pat_env) if args.github_pat_env else None
        if pat is None and args.github_pat_prompt:
            pat = getpass.getpass("GitHub PAT (niewidoczny, Enter = pomiń): ").strip() or None
        if pat:
            config = GatewayConfig.load(config_path)
            endpoint = next(i.endpoint for i in integrations if i.id == "com.github/remote")
            asyncio.run(AuthManager(open_store(config, warn=False)).set_secret(
                config.user_id, "com.github/remote", "default", pat, endpoint=endpoint))
            print("GitHub: PAT zapisany w zaszyfrowanym magazynie")
        else:
            print("GitHub: bez PAT — połączenie zwróci `connect_account`, dopóki go nie podasz (setup --github-pat-prompt)")

    command = [sys.executable, "-m", "mcpilot.gateway", "--config", str(config_path)]
    registration = {"host": args.register, "name": args.name, "scope": args.scope, "keychain_item": item,
                    "registered_at": datetime.now().isoformat(timespec="seconds"), "registered": False}
    if args.register == "claude-code":
        existing = _claude("mcp", "get", args.name)
        if existing is None:
            print("Claude Code: nie znaleziono polecenia `claude`; zarejestruj ręcznie:")
            print("  claude mcp add --scope user " + args.name + " -- " + " ".join(command))
        elif existing.returncode == 0:
            print(f"Claude Code: serwer `{args.name}` już istnieje — nie nadpisuję (uninstall albo --name)")
        else:
            result = _claude("mcp", "add", "--scope", args.scope, args.name, "--", *command)
            registration["registered"] = bool(result and result.returncode == 0)
            print(f"Claude Code: {'zarejestrowano' if registration['registered'] else 'rejestracja nieudana'} "
                  f"`{args.name}` (scope {args.scope}), bez sekretów w konfiguracji hosta")
    else:
        print("Polecenie bramy dla hosta MCP (bez sekretów):\n  " + " ".join(command))
    (directory / SETUP_FILE).write_text(json.dumps(registration, indent=2))
    print("\nDalej: w Claude Code poproś np. „Znajdź w Notion dokumentację projektu”. Przy pierwszym użyciu "
          "Notion otworzy się przeglądarka do logowania; kolejne sesje korzystają z zapisanego dostępu.")
    return 0


def _describe(record: dict[str, Any], mode: str) -> str:
    if mode == "none":
        return "bez logowania"
    if not record:
        return "brak połączenia"
    if mode in {"bearer", "api_key"}:
        return "PAT/klucz zapisany" if record.get("secret") else "brak połączenia"
    tokens = record.get("tokens") or {}
    if not tokens:
        return "brak połączenia (rejestracja klienta zapisana)" if record.get("client_info") else "brak połączenia"
    expires = record.get("expires_at")
    until = datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M") if expires else "bez terminu"
    scope = tokens.get("scope") or "—"
    return f"połączone (token do {until}, odświeżanie: {'tak' if tokens.get('refresh_token') else 'nie'}, scope: {scope})"


def status(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    config = GatewayConfig.load(config_path)
    key, source = resolve_key(config.key_env, config.keychain_item)
    print(f"Konfiguracja: {config_path.resolve()} (użytkownik {config.user_id})")
    print(f"Klucz: {source}")
    catalog = Catalog()
    catalog.load_manifest(config.manifest)
    store = open_store(config, warn=False)
    if key is None:
        print("Uwaga: bez klucza poświadczenia nie są trwałe; uruchom ponownie `python -m mcpilot setup`.")
    for integration in catalog.all():
        for account in config.accounts:
            record = asyncio.run(store.get(CredentialKey(config.user_id, integration.id, account,
                                                         integration.endpoint or "stdio"))) or {}
            print(f"  {integration.id:<22} [{account}] {integration.auth.mode:<6} {_describe(record, integration.auth.mode)}")
    return 0


def _find(catalog: Catalog, name: str):
    for integration in catalog.all():
        if name in {integration.id, integration.service}:
            return integration
    raise SystemExit(f"Nieznana integracja: {name}")


def disconnect(args: argparse.Namespace) -> int:
    config = GatewayConfig.load(Path(args.config).expanduser())
    catalog = Catalog()
    catalog.load_manifest(config.manifest)
    integration = _find(catalog, args.integration)
    store = open_store(config, warn=False)
    asyncio.run(AuthManager(store).revoke(config.user_id, integration.id, args.account,
                                          endpoint=integration.endpoint or "stdio"))
    print(f"{integration.id} [{args.account}]: lokalne poświadczenia usunięte; następne użycie wymaga ponownego połączenia.")
    if integration.auth.mode != "none":
        print("Cofnięcie u dostawcy (unieważnia wydany token): "
              + REVOKE_HELP.get(integration.service, "w ustawieniach dostawcy."))
    return 0


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def approve(args: argparse.Namespace) -> int:
    # Approval is a person's decision: a model running commands in the background has no terminal.
    if not _interactive():
        print("Zatwierdzenie wymaga terminala z osobą przy klawiaturze (stdin/stdout to nie TTY). "
              f"Uruchom samodzielnie: python -m mcpilot approve {args.server}", file=sys.stderr)
        return 2
    config_path = Path(args.config).expanduser().resolve()
    config = GatewayConfig.load(config_path)
    login = LoopbackLogin(config.user_id, port=config.login_port)
    try:
        record = asyncio.run(approve_entry(
            args.server, config_path=config_path, store=open_store(config, warn=False), user_id=config.user_id,
            ask=input, say=print, secret_prompt=getpass.getpass,
            login=OAuthConfig(login=login.broker, redirect_uri=login.redirect_uri), auth=args.auth,
            local=args.local, container=args.container, include_write=args.include_write,
            account=args.account, state_dir=config.state_dir, allow_loopback=config.allow_loopback,
            source=args.registry))
    except ApprovalError as exc:
        print(f"Nie można zatwierdzić: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # network/auth failures: class name only, never payloads
        print(f"Nie udało się połączyć z serwerem ({type(exc).__name__}). Nic nie zapisano.", file=sys.stderr)
        return 1
    finally:
        login.close()
    if record is None:
        return 1
    print(f"\nZatwierdzono {record['id']} ({len(record['tools'])} narzędzi, capability: "
          f"{', '.join(record['capabilities'])}). Brama wczyta zmianę przy następnym wyszukaniu narzędzi.")
    return 0


def remove(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = GatewayConfig.load(config_path)
    catalog = Catalog()
    catalog.load_manifest(config.manifest)
    integration = next((i for i in catalog.all() if i.id == args.server), None)
    if integration is None:
        print(f"Nie ma zatwierdzonej integracji {args.server}", file=sys.stderr)
        return 1
    store = open_store(config, warn=False)
    for account in config.accounts:
        asyncio.run(AuthManager(store).revoke(config.user_id, integration.id, account,
                                              endpoint=integration.endpoint or "stdio"))
    remove_entry(config_path, integration.id)
    print(f"Wycofano zatwierdzenie {integration.id} i usunięto lokalne poświadczenia. "
          "Token wydany przez dostawcę cofniesz w jego ustawieniach.")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    directory = Path(args.dir).expanduser().resolve()
    registration = json.loads((directory / SETUP_FILE).read_text()) if (directory / SETUP_FILE).exists() else {}
    if registration.get("registered"):
        result = _claude("mcp", "remove", "--scope", registration["scope"], registration["name"])
        print(f"Claude Code: {'usunięto' if result and result.returncode == 0 else 'nie udało się usunąć'} "
              f"`{registration['name']}`")
    item = registration.get("keychain_item")
    if item and not args.keep_key:
        print(f"Klucz mcpilot/{item}: {'usunięty' if keychain_delete(item) else 'nie znaleziono'} "
              "(zaszyfrowane poświadczenia stają się nieczytelne)")
    if args.purge and directory.exists():
        shutil.rmtree(directory)
        print(f"Usunięto katalog {directory}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mcpilot", description="MCPilot pilot kit (local gateway)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="configure Notion/GitHub (+files), keychain key, Claude Code registration")
    p.add_argument("--dir", default=str(DEFAULT_DIR))
    p.add_argument("--user", default=getpass.getuser(), help="local user label recorded in audit events")
    p.add_argument("--workspace", help="also allow read-only access to this local directory")
    p.add_argument("--no-notion", action="store_true")
    p.add_argument("--no-github", action="store_true")
    p.add_argument("--github-pat-env", help="read the GitHub PAT from this environment variable")
    p.add_argument("--github-pat-prompt", action="store_true", help="ask for the GitHub PAT without echo")
    p.add_argument("--github-endpoint", help="GitHub Enterprise or test endpoint")
    p.add_argument("--notion-endpoint", help="test endpoint")
    p.add_argument("--allow-loopback", action="store_true", help="development endpoints on 127.0.0.1")
    p.add_argument("--keychain-item")
    p.add_argument("--no-registry-sync", action="store_true", help="do not download MCP Registry suggestions")
    p.add_argument("--register", choices=["claude-code", "none"],
                   default="claude-code" if shutil.which("claude") else "none")
    p.add_argument("--scope", choices=["user", "local", "project"], default="user")
    p.add_argument("--name", default="mcpilot")
    p.set_defaults(handler=setup)

    p = sub.add_parser("approve", help="approve an MCP Registry server (read-only by default) after review")
    p.add_argument("server", help="registry name, e.g. com.atlassian/atlassian-mcp-server")
    p.add_argument("--config", default=str(DEFAULT_DIR / "gateway.json"))
    p.add_argument("--auth", choices=["oauth", "token"], help="remote servers: default OAuth unless a token is required")
    p.add_argument("--local", action="store_true", help="allow an npm package to run on this computer")
    p.add_argument("--container", action="store_true", help="run a package only inside Docker")
    p.add_argument("--include-write", action="store_true", help="also map tools not marked read-only (asks again)")
    p.add_argument("--account", default="default")
    p.add_argument("--registry", default=OFFICIAL_REGISTRY, help=argparse.SUPPRESS)
    p.set_defaults(handler=approve)

    p = sub.add_parser("remove", help="withdraw the approval of an integration")
    p.add_argument("server")
    p.add_argument("--config", default=str(DEFAULT_DIR / "gateway.json"))
    p.set_defaults(handler=remove)

    p = sub.add_parser("status", help="connection state per integration (no tokens shown)")
    p.add_argument("--config", default=str(DEFAULT_DIR / "gateway.json"))
    p.set_defaults(handler=status)

    p = sub.add_parser("disconnect", help="remove the local grant for an integration")
    p.add_argument("integration", help="integration id or service name, e.g. notion")
    p.add_argument("--account", default="default")
    p.add_argument("--config", default=str(DEFAULT_DIR / "gateway.json"))
    p.set_defaults(handler=disconnect)

    p = sub.add_parser("uninstall", help="unregister from Claude Code and delete the keychain key")
    p.add_argument("--dir", default=str(DEFAULT_DIR))
    p.add_argument("--keep-key", action="store_true")
    p.add_argument("--purge", action="store_true", help="also delete the configuration directory")
    p.set_defaults(handler=uninstall)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
