# Zgodność hostów MCP

Stan na **2026-10-05**. „Zweryfikowane” oznacza przebieg w prawdziwym hoście z dowodem w dzienniku audytu bramy (`spec/hosts/`). „Dokumentacja” oznacza deklarację producenta hosta, bez przebiegu.

## Macierz

| Host | Brama stdio | Brama HTTP + token hosta | OAuth hosta do bramy (PRM) | Elicytacja URL (logowanie u dostawców) | Status |
| --- | --- | --- | --- | --- | --- |
| Claude Code 2.1.284 (CLI, `-p`) | **zweryfikowane**: `find_tools` → `call_tool` → odczyt | **zweryfikowane**: nagłówek `Authorization`, tenant w audycie | nie testowano | nie testowano (tryb `-p` jest nieinteraktywny) | E2E lokalnie |
| Cursor | dokumentacja: `mcpServers` z `command`/`args`/`env` | dokumentacja: `url` + `headers` (także `${env:…}`) | dokumentacja: OAuth, callback `http://localhost:8787/callback` | dokumentacja: „Elicitation — Supported”, bez rozróżnienia trybu formularza i URL | nie zweryfikowano |
| VS Code (Copilot) | dokumentacja: `.vscode/mcp.json`, obiekt `servers` | dokumentacja: `type: http`, `url`, `headers` | dokumentacja: OAuth, wstępnie zarejestrowany `client_id` | nie znaleziono w dokumentacji (szukano w dwóch stronach podanych niżej) | nie zweryfikowano |

**Dowody:**
- `spec/hosts/claude-code-2.1.284-stdio-2026-10-05.jsonl`: `connect ready` 302 ms, `call ok read_file` 9 ms.
- `spec/hosts/claude-code-2.1.284-http-2026-10-05.jsonl`: tenant `acme`, użytkownik `alice`, `call ok read_file`; host zwrócił pierwszą linię pliku `# Project Atlas`.

**Źródła dokumentacji (2026-10-05):**
- Cursor: [cursor.com/docs/mcp](https://cursor.com/docs/mcp), [forum: zmiana callback OAuth](https://forum.cursor.com/t/oauth-redirect-uri-changed-from-cursor-to-http-localhost-for-streamable-http-mcp/165019)
- VS Code: [MCP servers](https://code.visualstudio.com/docs/agent-customization/mcp-servers), [MCP developer guide](https://code.visualstudio.com/api/extension-guides/ai/mcp), [MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)

## Co z tego wynika dla pilotażu

- **Claude Code** można dziś deklarować jako host pilotażu dla obu bram, z logowaniem przez kartę przeglądarki otwieraną przez bramę lokalną albo przez `connect_page` bramy zdalnej.
- **Cursor i VS Code** wymagają ręcznego przebiegu według checklisty poniżej, zanim trafią do oferty.
- **Logowanie jednym kliknięciem przez elicytację URL nie jest potwierdzone w żadnym hoście.** Dopóki tak jest, w bramie zdalnej użytkownik loguje się przez `connect_page`, a w lokalnej przez kartę przeglądarki.

## Konfiguracja

`python examples/gateway_setup.py` wypisuje gotowe wpisy dla Claude Code, Cursor (`.cursor/mcp.json`, `mcpServers`) i VS Code (`.vscode/mcp.json`, `servers`) z bezwzględnymi ścieżkami.

## Powtórzenie weryfikacji

Claude Code, bez zmiany konfiguracji użytkownika (jednorazowa konfiguracja MCP; zużywa limit konta Claude):

```sh
python examples/gateway_setup.py /tmp/mcpilot-host        # potem dopisz "audit_log": "audit.jsonl" w gateway.json
cat > /tmp/mcpilot-host/mcp.json <<JSON
{"mcpServers": {"mcpilot": {"command": "$(pwd)/.venv/bin/python",
  "args": ["-m", "mcpilot.gateway", "--config", "/tmp/mcpilot-host/gateway.json"]}}}
JSON
claude -p "Use mcpilot_find_tools with task 'Przeczytaj lokalny plik README', then mcpilot_call_tool to read README.md. Reply with only the first line." \
  --mcp-config /tmp/mcpilot-host/mcp.json --strict-mcp-config \
  --allowedTools mcp__mcpilot__mcpilot_find_tools mcp__mcpilot__mcpilot_call_tool --max-turns 6
cat /tmp/mcpilot-host/audit.jsonl                           # oczekiwane: connect ready, call ok read_file
```

### Checklista ręczna (Cursor, VS Code, inne hosty)

Każdy punkt zapisz jako wynik oraz wersję hosta. Do dziennika audytu dołącz plik do `spec/hosts/<host>-<wersja>-<transport>-<data>.jsonl`.

1. **Brama stdio:** dodaj wpis z `gateway_setup.py` z `audit_log` w `gateway.json`. W czacie agenta poproś o odczyt README przez MCPilot. Oczekiwane w audycie: `connect ready`, `call ok read_file`.
2. **Logowanie przez kartę przeglądarki (brama lokalna):** w manifeście z Notion poproś o wyszukanie w Notion. Oczekiwane: otwiera się przeglądarka, model dostaje `finish_login`, a po zalogowaniu kolejne `mcpilot_find_tools` zwraca narzędzia.
3. **Brama HTTP:** uruchom bramę zdalną, skonfiguruj `url` i nagłówek `Authorization`. Oczekiwane w audycie: tenant i użytkownik z tokenu.
4. **OAuth hosta do bramy:** usuń nagłówek. Oczekiwane: host odczytuje Protected Resource Metadata i rozpoczyna logowanie u IdP. Wymaga IdP organizacji.
5. **Elicytacja URL:** narzędzie wymagające OAuth u dostawcy. Oczekiwane: host pokazuje prośbę z linkiem `/connect/…`. Zapisz, czy host w ogóle pokazał prośbę, czy model dostał tylko `finish_login` z `connect_page`.
6. **Limit czasu wywołań:** czy host przerwał `mcpilot_find_tools` w trakcie logowania (`connect_wait`/`login_wait`)?
