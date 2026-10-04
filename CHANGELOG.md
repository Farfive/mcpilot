# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/), wersjonowanie semantyczne (przed 1.0 drugi człon oznacza zmiany niezgodne wstecz).

## [0.1.0] — niewydane (pilotaż)

### Dodane
- SDK Python: katalog (MCP Registry: synchronizacja pełna i przyrostowa), polityka, router, OAuth MCP z przyciskiem logowania (`LoginBroker`), sesje stdio i Streamable HTTP, workflow ze wznowieniem, warstwa discovery, audyt.
- Brama lokalna (stdio) i zdalna (Streamable HTTP, wielu użytkowników, tenanty, JWT, logowanie przez elicytację URL).
- TypeScript SDK (`ts/`) z tym samym kontraktem danych, potwierdzonym wspólnymi wektorami (`spec/vectors.json`).
- Budżet kroków i kosztu liczony per zadanie (`ToolSet.task_id`); wcześniej długie sesje bramy blokowały się po 20 wywołaniach.
- Zdarzenia audytu z `tenant`, `task_id`, `duration_ms`; `JsonlAuditSink` i `audit_log` w konfiguracji obu bram; metryki pilotażu `python -m mcpilot.metrics`.
- Skrypt protokołu pilotażu `python -m mcpilot.pilot` z raportem bez sekretów; diagnostyka mapowania `MCPilot.diagnose`.
- Migawki narzędzi dostawców (`spec/providers/`) i dowody zgodności hostów (`spec/hosts/`, `docs/hosts.md`).
- CI (Python 3.11–3.13, Node 20/22), artefakty z `SHA256SUMS`.

### Zmienione (niezgodne wstecz)
- Odcisk manifestu i skrót klucza poświadczeń liczone z RFC 8785 (zgodne z TypeScript). Poświadczenia zapisane wcześniej w `EncryptedFileSecretStore` trzeba połączyć ponownie.
- `issue_read` w GitHub MCP wymaga parametru `method` (zgodnie z `github-mcp-server` v1.14.0); fixture i przykłady zaktualizowane.
