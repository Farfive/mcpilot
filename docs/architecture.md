# Architektura

MCPilot to biblioteka uruchamiana w procesie aplikacji hosta. Host dostarcza model, pętlę agenta, uwierzytelnionego użytkownika i interfejs logowania. Biblioteka dobiera integracje MCP, zarządza połączeniami i autoryzacją, a modelowi udostępnia wyłącznie wybrane narzędzia. Import nie instaluje pakietów, nie otwiera gniazd, nie tworzy katalogów i nie uruchamia procesów.

## Ścieżka główna

```
zadanie ─▶ requirements() ─▶ route() ─▶ Plan ─▶ connect() ─▶ tools_for() ─▶ call()
            capabilities      kandydaci    wybór     auth +       schematy     walidacja,
            usługa, konto     polityka     powody    sesja MCP    budżet       limity, audyt
```

1. **Wymagania.** `router.requirements()` rozpoznaje usługi (aliasy PL/EN) i capabilities z treści zadania. Jawne `TaskRequest.services`, `capabilities` i `accounts` mają pierwszeństwo przed heurystyką. Dla wskazanej usługi bez rozpoznanej capability router wybiera tylko capabilities o skutku `read` dopuszczone przez politykę.
2. **Kandydaci.** `route()` bierze wyłącznie manifesty z zaufanego katalogu hosta. Odrzuca integracje bez wspieranego adaptera, spoza polityki, bez mapowania narzędzi lub dla niedozwolonego konta. Każde odrzucenie trafia do `Plan.unavailable` z kodem (`setup_required`, `unsupported`, `policy_denied`, `no_tool_mapping`).
3. **Ocena.** Deterministyczny wynik: `100 + 20·istniejące_połączenie + 10·jakość − koszt − opóźnienie`, remis rozstrzyga ID. Opcjonalny reranker LLM dostaje maksymalnie 10 kwalifikujących się kandydatów (ID, usługa, capabilities, wynik) i zwraca ID. Kod przyjmuje wyłącznie ID z tej listy; inny wynik daje `InvalidProposal`. Model nie może podać adresu, pakietu ani polecenia.
4. **Połączenie.** `connect()` sprawdza politykę, przygotowuje poświadczenia (`AuthManager.prepare`), wykonuje fazę autoryzacji przed sesją (logowanie może trwać minuty), otwiera sesję stdio lub Streamable HTTP, pobiera `tools/list`, waliduje schematy i zapamiętuje tylko narzędzia zmapowane przez hosta do żądanych capabilities i dopuszczonych skutków.
5. **Narzędzia.** `tools_for()` łączy brakujące integracje, ponownie używa gotowych i zwraca `ToolSet` w budżecie tokenów. Identyfikator `mcp_<sha256(connection:tool)>` jest jednoznaczny między serwerami i kontami.
6. **Wywołanie.** `call()` przyjmuje tylko ID wydane w tej sesji użytkownika, ponownie sprawdza politykę, waliduje argumenty JSON Schema, egzekwuje limity kroków, kosztu i rozmiaru wyniku oraz waliduje `structuredContent` względem `outputSchema`. Limity kroków i kosztu (`Limits`) dotyczą **zadania**: każde `tools_for` (w bramie każde `mcpilot_find_tools`) otwiera nowe zadanie z identyfikatorem `ToolSet.task_id`, a wywołania jego narzędzi obciążają jego budżet. Długo żyjąca sesja (np. użytkownik bramy) nie wyczerpuje więc limitu na zawsze.

## Komponenty

| Moduł | Odpowiedzialność |
| --- | --- |
| `models.py` | Niezmienne kontrakty Pydantic bez sekretów: `Integration`, `ToolRule`, `Plan`, `Connection`, `Tool`, `ToolSet`, `CallResult`, `AuditEvent`, `Limits`. |
| `catalog.py` | Zaufane manifesty hosta oddzielone od niezaufanych wpisów rejestrów; synchronizacja pełna i przyrostowa, cache offline. Szczegóły: [catalog.md](catalog.md). |
| `policy.py` | Migawka decyzji hosta: odcisk SHA-256 zatwierdzonego manifestu, capabilities, skutki, konta, reguły endpointów. |
| `router.py` | Wymagania z kontekstu, filtrowanie, ocena, ograniczony reranker, powody niedostępności. |
| `auth.py` | `AuthManager`, magazyny sekretów, OAuth przez oficjalne SDK MCP, `LoginBroker` (przycisk logowania). Szczegóły: [auth.md](auth.md). |
| `runtime.py`, `installer.py` | Sesje MCP z jednym właścicielem cyklu życia, timeouty, health check, zamykanie procesów; instalacja przypiętych pakietów do osobnych środowisk. |
| `sdk.py` | Publiczne API `MCPilot`. |
| `workflow.py` | Trwałe workflow wielu integracji, wznowienie po logowaniu, stany niepewne. |
| `discovery.py` | Dwie meta-funkcje dla modelu zamiast katalogu. |
| `gateway.py` | Brama MCP przez stdio dla jednego użytkownika i CLI obu wariantów bramy. Szczegóły: [gateway.md](gateway.md). |
| `remote.py` | Zdalna brama Streamable HTTP dla wielu użytkowników: autoryzacja MCP hosta (JWT), tenanty, sesje per użytkownik, limity, logowanie przez elicytację URL. |
| `canonical.py` | RFC 8785 (kanoniczny JSON) dla odcisków manifestów i kluczy poświadczeń, identyczny z TypeScript. |
| `ts/` | TypeScript SDK z tym samym kontraktem danych. Szczegóły: [typescript.md](typescript.md). |
| `adapters/langchain.py` | Adapter LangChain Core (`StructuredTool`). |

## Modele danych

**`Integration`** (manifest zatwierdzany przez hosta): `id`, `service`, `publisher`, `version`, `transport` (`http`/`stdio`), `endpoint` albo `command`/`package` (`PackageSpec`: runtime, nazwa, dokładna wersja, entrypoint, argumenty, przypięte zależności), `capabilities`, `tools: {nazwa: ToolRule}`, `auth: AuthSpec`, `support`, `source`, `quality`, `cost_per_call`, `latency_ms`. Walidator odrzuca niespójny transport, np. HTTP z poleceniem.

**`ToolRule`** to zaufane mapowanie narzędzia na capability i skutek (`read`, `draft`, `write`, `send`) oraz opcjonalny parametr idempotencji. Adnotacje serwera MCP nie nadają uprawnień.

**`AuthSpec`**: tryb `none`/`oauth`/`bearer`/`api_key`, minimalne scopes per capability, komunikat `setup_required`, nazwa nagłówka lub zmiennej środowiskowej, opis autoryzacji serwera do usługi docelowej.

**`Plan`**: `selections` (integracja, capability, konto, wynik, powód), `missing` (niespełnione wymagania), `unavailable` (znane integracje z kodem przyczyny).

**`Connection`**: `id` (pochodna użytkownika, integracji, konta i endpointu), `status`, przyznane `capabilities`, bezpieczny komunikat. Nie zawiera tokenów ani URL logowania.

**`Tool` / `CallResult`** mają stałe `untrusted=True`: opisy i wyniki serwerów są danymi, a nie instrukcjami.

### Stany integracji

| Stan | Gdzie widoczny | Znaczenie |
| --- | --- | --- |
| `discovered` | `Catalog.discovered()`, `Integration.support` | Metadane z rejestru lub manifest bez adaptera. Niewykonywalne. |
| `supported` / `experimental` | `Integration.support` | Host ma adapter i mapowanie narzędzi. |
| `approved` | `Policy.check()` | Dokładny manifest (odcisk) dopuszczony przez politykę. |
| `auth_required` | `Connection.status` | Użytkownik musi połączyć konto lub rozszerzyć zgodę. |
| `setup_required` | `Connection.status`, `Plan.unavailable` | Potrzebna aplikacja OAuth, zgoda administratora lub konfiguracja hosta. |
| `connected` | `Connection.status` | Sesja działa, ale żadne narzędzie nie spełnia mapowania. |
| `ready` | `Connection.status` | Narzędzia potwierdzone przez `tools/list` i zwalidowane. |
| `disconnected` / `error` | `Connection.status` | Sesja zamknięta albo zsanityzowany błąd. |

## API SDK

```python
MCPilot(*, user_id, catalog, policy, auth=None, runtime=None, state_dir=".mcpilot",
        limits=None, reranker=None, token_counter=None, audit=None)
```

| Metoda | Wynik |
| --- | --- |
| `await discover(task, limit=5)` | Małe podsumowania wybranych integracji, bez endpointów i schematów. |
| `await plan(task)` | `Plan`. `task` to tekst albo `TaskRequest`. |
| `await connect(integration_id, account="default", capabilities=None)` | `Connection`. |
| `await tools_for(task_or_plan, token_budget=None)` | `ToolSet` z narzędziami, połączeniami i informacją o obcięciu. |
| `await call(tool_id, arguments, idempotency_key=None)` | `CallResult`. |
| `await status(integration_id=None)` | Krotka `Connection`. |
| `await diagnose(integration_id, account="default")` | Diagnostyka dla hosta: narzędzia serwera kontra manifest (obecne, brakujące, niezmapowane). Nie dla modelu. |
| `await disconnect(integration_id, account="default", revoke=False)` | Zamyka sesję; `revoke=True` usuwa lokalne poświadczenia. |
| `await close()` / `async with` | Zamyka wszystkie sesje i procesy. |

Jeden obiekt `MCPilot` obsługuje jednego użytkownika uwierzytelnionego przez hosta. Model nie wybiera `user_id`.

Wyjątki (`errors.py`) mają bezpieczne komunikaty: `PolicyDenied`, `InvalidArguments`, `BudgetExceeded`, `ConnectionUnavailable`, `UncertainOutcome`, `InvalidProposal`.

## Kontekst LLM

Model nie dostaje katalogu. Host ma dwie drogi:

- **Bezpośrednio:** `tools_for()` zwraca tylko narzędzia wybrane dla zadania, w budżecie `Limits.tool_tokens` (domyślny licznik liczy bajty UTF-8, czyli konserwatywnie). Host może podać własny `token_counter`.
- **Warstwa discovery:** `DiscoveryTools` udostępnia `mcpilot_find_tools` (zadanie, opcjonalnie nazwy usług) i `mcpilot_call_tool` (ID narzędzia, argumenty). Schematy pobierane są tylko dla wybranych integracji. Argumenty meta-narzędzi są walidowane, a nieznane pola odrzucane. Odpowiedź zawiera statusy i kody akcji (`connect_account`, `finish_login`, `admin_setup`), nigdy URL, tokeny ani endpointy. Przy `connect_wait` logowanie trwające dłużej daje `auth_pending` i kończy się w tle, więc pętla agenta nie stoi.

Gdy aliasy routera nie rozpoznają usługi, `mcpilot_find_tools` przeszukuje cały kontekst (`task` + opcjonalne `context`) indeksem BM25 z obsługą polskiej odmiany i intencji dostawcy, a dla braków zwraca sugestie z rejestru do zatwierdzenia przez administratora: [search.md](search.md).

Opisy narzędzi są obcinane do 2000 znaków. Wyniki ograniczają `max_result_bytes`. Odpowiedzi zawierają `untrusted: true` i informację, że nie zmieniają zasad aplikacji. Uprawnienia egzekwuje kod polityki, niezależnie od treści przekazanej modelowi.

## Wykonywanie i workflow

`WorkflowRunner` zapisuje plan kroków w SQLite (plik 0600) przed wykonaniem. Każdy krok przechodzi `pending → running → complete | failed | uncertain`. Stan kroku zapisuje się przed operacją sieciową razem z rezerwacją kroków i kosztu.

- Brak autoryzacji kończy przebieg stanem `waiting_for_auth`; po zalogowaniu `resume()` kontynuuje od pierwszego niewykonanego kroku.
- Odczyty mają ograniczone retry. Zapis lub wysyłka przerwane timeoutem albo awarią procesu stają się `uncertain` i nigdy nie są powtarzane automatycznie. `reconcile()` pozwala hostowi zapisać wynik potwierdzony u dostawcy.
- Klucz idempotencji jest przekazywany tylko do parametru wskazanego w `ToolRule`.
- Przygotowanie wiadomości (`draft`) i wysłanie (`send`) to różne skutki. Domyślna polityka dopuszcza `read` i `draft`. Działania mieszczące się w polityce wykonują się bez kolejnych potwierdzeń; nowe scope wymagają zmiany polityki i nowej zgody.
- Blokada pliku zapobiega równoległemu wykonaniu tego samego przebiegu. Przebieg innego użytkownika jest niedostępny.

## Granice bezpieczeństwa

- Wpisy rejestru nigdy nie stają się wykonywalne. Endpoint, pakiet i polecenie pochodzą wyłącznie z manifestu hosta, a zmiana manifestu unieważnia zatwierdzenie (odcisk).
- Zdalne endpointy wymagają HTTPS i publicznego adresu; loopback tylko po jawnym `allow_loopback`. Klient HTTP nie podąża za przekierowaniami i ignoruje proxy ze środowiska.
- Procesy lokalne dostają oczyszczone środowisko z własnym `HOME`; poświadczenia trafiają tylko jawnie. Instalator przypina wersje, wyłącza skrypty npm i instaluje Pythona wyłącznie z kół. To izolacja zależności, nie sandbox systemu operacyjnego.
- Sekrety nie trafiają do modelu, wyjątków, logów, `repr` ani zdarzeń audytu. Logi OAuth SDK są redagowane.
- Zdarzenia audytu (`audit=`) zawierają akcję, wynik, integrację, konto, narzędzie, skutek, `tenant`, `task_id` i `duration_ms`, bez argumentów i wyników. Awaria odbiornika audytu nie zamienia wykonanego zapisu w błąd. `JsonlAuditSink` zapisuje je do pliku 0600, a `python -m mcpilot.metrics` liczy z nich metryki pilotażu.

## Ograniczenia MVP

Router bazowy jest heurystyczny; dla szerokiego katalogu host powinien dodać reranker lub własne `TaskRequest`. Stan sesji jest w pamięci procesu; trwały jest stan workflow i poświadczeń. W Pythonie weryfikatory PKCE trwającego logowania nie przetrwają restartu procesu (potrzebne jest ponowne kliknięcie); w TypeScript weryfikator jest w magazynie sekretów, ale oczekujący przycisk nadal żyje w pamięci `LoginBroker`. Zdalna brama trzyma oczekujące logowania i procesy stdio w pamięci instancji, więc wiele instancji wymaga przyklejonych sesji.

## Zgodność Python ↔ TypeScript

Kontrakt danych (manifesty, plany, połączenia, narzędzia, workflow) używa tych samych pól `snake_case` w obu SDK. Odcisk manifestu, skrót klucza poświadczeń, identyfikator połączenia i identyfikator narzędzia wynikają z SHA-256 kanonicznego JSON (RFC 8785), więc są identyczne. `scripts/export_vectors.py` zapisuje wektory z implementacji Python do `spec/vectors.json`; `tests/test_vectors.py` pilnuje ich aktualności, a `ts/test/vectors.test.ts` weryfikuje TypeScript. Różnice w zachowaniu opisuje [typescript.md](typescript.md#różnice).
