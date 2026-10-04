# Pilotaż na prawdziwych kontach

Ten katalog zawiera raporty przebiegów według protokołu z [acceptance.md](../acceptance.md#wymaga-kont-dostawców). Dopóki nie ma tu raportu z werdyktem `pass` lub `pass_without_provider_revoke` i statusem dowodu „Konto dostawcy”, integracja pozostaje „Wymaga konta”.

## Definicja integracji wspieranej

Integracja może dostać w szablonie `support="supported"` dopiero wtedy, gdy:

1. Istnieje raport z przebiegu na koncie dostawcy (nie fixture), nie starszy niż ostatnia zmiana manifestu (odcisk w raporcie = odcisk zatwierdzonego manifestu).
2. Wszystkie kroki protokołu mają `pass`, z wyjątkiem „odświeżenie tokenu” = `n/a` dla PAT, a „cofnięcie u dostawcy” zostało potwierdzone (`--interactive`).
3. Krok „mapowanie narzędzi” nie ma `zmapowane brakujące`; migawka nazw narzędzi jest zapisana w `spec/providers/`.
4. Używane są wyłącznie narzędzia o skutku `read` (MVP jest odczytowe).

Pojedynczy przebieg potwierdza konto, nie wszystkie konfiguracje dostawcy. Raport podaje datę i wersję MCPilot.

## Czego potrzeba od właściciela kont (BIZ-2)

| Dostawca | Konto | Uprawnienia | Uwagi |
| --- | --- | --- | --- |
| Notion | Workspace z co najmniej jedną stroną dokumentacji testowej | Użytkownik, który może połączyć Notion MCP (OAuth) | Logowanie w przeglądarce na komputerze uruchamiającym skrypt; redirect `http://127.0.0.1:8765/callback` (akceptacja loopback przez Notion jest częścią testu) |
| GitHub | Repozytorium testowe z issue | Token PAT. Serwer `github-mcp-server` v1.14.0 zgłasza dla narzędzi issue scope klasyczny `repo` (`github-mcp-server list-scopes`) | Token wyłącznie przez zmienną środowiskową, nigdy w pliku ani raporcie |

Zgoda na przetwarzanie: skrypt nie zapisuje treści, ale podczas przebiegu wyniki narzędzi przechodzą przez proces MCPilot w pamięci. Na kontach używamy danych testowych.

## Uruchomienie

```sh
export MCPILOT_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
python examples/gateway_setup.py .mcpilot/pilot-config        # manifest z szablonami notion/github

# Notion: przeglądarka otworzy się do logowania; potem skrypt poprosi o odłączenie połączenia w Notion
python -m mcpilot.pilot --manifest .mcpilot/pilot-config/approved-integrations.json \
  --integration com.notion/mcp --tool notion-search --arguments '{"query": "<słowo z testowej strony>"}' \
  --interactive --report docs/pilots/$(date +%F)-notion.md

# GitHub: PAT w zmiennej; potem skrypt poprosi o usunięcie tokenu w ustawieniach GitHub
export GITHUB_PAT=...
python -m mcpilot.pilot --manifest .mcpilot/pilot-config/approved-integrations.json \
  --integration com.github/remote --tool search_issues --arguments '{"query": "repo:<org>/<repo> is:issue"}' \
  --secret-env GITHUB_PAT --interactive --report docs/pilots/$(date +%F)-github.md
```

Kod wyjścia `1` oznacza werdykt `fail`; raport i tak powstaje. Szablon raportu: [TEMPLATE.md](TEMPLATE.md).
