# MCPilot (PL)

> English version: [README.md](../README.md)

Asynchroniczne Python SDK, które daje agentowi LLM dostęp do serwerów Model Context Protocol: zadanie → dobór integracji → autoryzacja → tylko potrzebne narzędzia → wykonanie. Host dostarcza model, pętlę agenta, uwierzytelnionego użytkownika i interfejs logowania. Gdy brakuje dostępu, użytkownik dostaje przycisk połączenia konta, a zadanie wznawia się po zalogowaniu.

Import biblioteki nie instaluje pakietów, nie otwiera połączeń i nie uruchamia procesów. MVP korzysta z oficjalnego `mcp==2.3.0` i Python 3.11+.

## Uruchomienie

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
scripts/ci.sh        # pełny zestaw jak w CI: ruff, pytest, testy TypeScript, artefakty w dist/
```

**Pilotaż w Claude Code** (Notion + GitHub, sekrety w pęku kluczy, nie w konfiguracji hosta):

```sh
pip install './mcpilot-0.1.0-py3-none-any.whl[keychain]'   # wheel z artefaktów CI; z repozytorium: pip install -e '.[keychain]'
python -m mcpilot setup --github-pat-prompt
python -m mcpilot status
```

Szczegóły: [docs/pilots/README.md](pilots/README.md).

| Przykład | Co pokazuje | Status |
| --- | --- | --- |
| `python examples/local_task.py` | Zadanie → lokalny serwer stdio → odczyt pliku | E2E lokalnie |
| `python -m examples.notion_github_task` | Flagowe zadanie Notion + GitHub: wcześniej podłączone konto, przycisk logowania, wznowienie workflow, szkic podsumowania, ponowne użycie po restarcie | Demonstracyjne |
| `python -m examples.oauth_demo` | OAuth (discovery, DCR, PKCE) przez Streamable HTTP i ponowne użycie tokenu | Demonstracyjne |
| `python examples/langchain_tools.py` | Adapter LangChain (`pip install -e '.[langchain]'`) | E2E lokalnie |
| `python -m examples.claude_agent` | Pętla agenta z Claude na dwóch meta-narzędziach (`pip install -e '.[agent]'`) | Wymaga klucza API |
| `python examples/gateway_setup.py` | Konfiguracja lokalnej bramy MCP (stdio) dla Claude Code i hostów `mcpServers` | E2E lokalnie (filesystem), konta dla GitHub/Notion |
| `python -m examples.remote_gateway_demo` | Zdalna brama dla wielu użytkowników: token hosta, izolacja, logowanie przez elicytację URL, odrzucenie przekazanego linku | Demonstracyjne |
| `python -m mcpilot.pilot --manifest … --integration … --tool …` | Protokół pilotażu na prawdziwym koncie (logowanie, odczyt, ponowne użycie, odświeżenie, cofnięcie) z raportem bez sekretów | Wymaga konta ([docs/pilots](pilots/README.md)) |
| `python -m mcpilot.metrics audit.jsonl` | Metryki pilotażu z dziennika audytu bram (`audit_log`) | E2E lokalnie |
| `cd ts && npm install && npm test && npm run example` | TypeScript SDK: wektory zgodności z Pythonem, stdio/HTTP z serwerami Python, OAuth, klient TS ↔ brama Python | E2E lokalnie / Demonstracyjne |

**E2E lokalnie**: prawdziwe procesy i protokół, bez usług zewnętrznych. **Demonstracyjne**: prawdziwe MCP/HTTP/OAuth, symulowany dostawca, zgoda użytkownika lub model. **Wymaga konta**: kod gotowy, brak testu na koncie dostawcy. Pełna lista kryteriów z dowodami: [docs/acceptance.md](acceptance.md).

## API

```python
from mcpilot import MCPilot, Policy
from mcpilot.catalog import Catalog
from mcpilot.integrations import filesystem

integration = filesystem("./examples/workspace")
policy = Policy(approved=[integration], capabilities=["files.read"])

async with MCPilot(user_id="user-123", catalog=Catalog([integration]), policy=policy) as pilot:
    plan = await pilot.plan("Przeczytaj lokalny plik README")
    tools = await pilot.tools_for(plan)          # tylko narzędzia dla tego zadania
    reader = next(t for t in tools.tools if t.name == "read_file")
    result = await pilot.call(reader.id, {"path": "README.md"})
```

Publiczne API: `discover`, `plan`, `connect`, `tools_for`, `call`, `status`, `disconnect`. Ponadto `WorkflowRunner` (trwałe zadania wielu integracji ze wznowieniem), `LoginBroker` (przycisk logowania i trasa callback hosta), `DiscoveryTools` (dwa meta-narzędzia dla modelu zamiast katalogu), `python -m mcpilot.gateway` (brama MCP: lokalna stdio albo zdalna HTTP dla wielu użytkowników) oraz [TypeScript SDK](typescript.md) w `ts/` z tym samym kontraktem danych. Jeden obiekt `MCPilot` należy do jednego użytkownika zweryfikowanego przez hosta; model nie ustala `user_id`, adresów ani poleceń.

## Status funkcji

| Obszar | Działa | Uwagi |
| --- | --- | --- |
| Transporty stdio i Streamable HTTP | E2E lokalnie | Timeouty, health check, zamykanie procesów, oczyszczone środowisko |
| Instalacja przypiętych pakietów | E2E sieć (oficjalny filesystem z npm) | Izolacja zależności, nie sandbox systemu |
| Katalog: oficjalny Registry, prywatne rejestry, cache | E2E sieć (40 209 wpisów, synchronizacja przyrostowa) | Wpisy rejestru nigdy nie są wykonywalne bez manifestu hosta |
| Router z kontekstu, polityka, reranker | E2E lokalnie | Reranker może wybrać tylko kwalifikującego się kandydata |
| OAuth MCP, PAT/klucz API, odświeżanie, cofanie, wiele kont | Demonstracyjne / E2E lokalnie | Prawdziwe logowanie Notion i PAT GitHub: wymaga konta |
| Workflow, limity, brak powtórzeń zapisu | E2E lokalnie / Demonstracyjne | Niepewne mutacje wymagają rekoncyliacji |
| Brama MCP lokalna (stdio) | E2E lokalnie | Jeden użytkownik, przeglądarka + callback na loopback; zweryfikowana w Claude Code ([hosty](hosts.md)) |
| Brama MCP zdalna (HTTP, wielu użytkowników) | E2E lokalnie / Demonstracyjne | JWT hosta, tenanty, limity, elicytacja URL; IdP organizacji: wymaga konta |
| TypeScript SDK | E2E lokalnie / Demonstracyjne | Zgodność z Pythonem potwierdzona wspólnymi wektorami |
| Zatwierdzanie serwerów z rejestru (`python -m mcpilot approve`) | E2E lokalnie / E2E sieć | Tylko odczyt domyślnie, wymagany terminal; Microsoft Learn z prawdziwego rejestru ([zatwierdzanie](approval.md)) |
| Google Drive, Slack | `setup_required` | Wymagają aplikacji OAuth i zgody administratora |

## Dokumentacja

- [Architektura, modele danych, API, kontekst LLM, bezpieczeństwo](architecture.md)
- [Autoryzacja, przepływ logowania, integracja interfejsu hosta](auth.md)
- [Katalog i synchronizacja](catalog.md)
- [Weryfikacja GitHub, Notion, Google Drive, Slack i filesystem](integrations.md)
- [Brama MCP (lokalna i zdalna), elicytacja URL, konfiguracja hostów](gateway.md)
- [TypeScript SDK i zgodność z Pythonem](typescript.md)
- [Dobór MCP z kontekstu rozmowy (algorytm, pomiary)](search.md)
- [Zatwierdzanie serwerów z MCP Registry jednym poleceniem](approval.md)
- [Zgodność hostów MCP (Claude Code, Cursor, VS Code)](hosts.md)
- [Pilotaż na prawdziwych kontach: definicja integracji wspieranej, raporty](pilots/README.md)
- [Zmiany](../CHANGELOG.md) i [bezpieczeństwo](../SECURITY.md)
- [Kryteria odbioru i status weryfikacji](acceptance.md)
- [Model biznesowy i plan wydań](product.md)

Nie deklarujemy pełnego pokrycia serwerów MCP. Manifest hosta określa endpoint lub pakiet, przypiętą wersję, mapowanie narzędzi na uprawnienia i skutki ich użycia; dopiero wtedy integracja staje się wykonywalna.
