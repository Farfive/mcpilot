# TypeScript SDK

`ts/` zawiera `@mcpilot/sdk` — ten sam silnik co Python: katalog, polityka, router, autoryzacja, sesje MCP, warstwa discovery i workflow. Bazuje na oficjalnym `@modelcontextprotocol/client@2.3.0` (protokół 2026-07-28 z negocjacją wersji) i działa w Node 20+. Import nie wykonuje I/O, nie uruchamia procesów i nie otwiera połączeń.

```sh
cd ts
npm install
npm test          # build + 17 testów, w tym z prawdziwymi serwerami Python
npm run example   # zadanie → lokalny serwer stdio → wywołanie
```

## Użycie

```ts
import { Catalog, MCPilot, parseIntegration, Policy, Runtime, AuthManager, LoginBroker } from "@mcpilot/sdk";
import { EncryptedFileSecretStore } from "@mcpilot/sdk/node";

const notion = parseIntegration(manifestJson);        // ten sam JSON manifestu co w Pythonie
const broker = new LoginBroker((request) => ui.showConnectButton(request.userId, request.url));
const auth = new AuthManager(new EncryptedFileSecretStore(".mcpilot/credentials", key), {
  login: broker, redirectUri: "https://app.example.com/oauth/mcp/callback",
});
const pilot = new MCPilot({
  userId: session.userId, catalog: new Catalog([notion]),
  policy: new Policy([notion], { capabilities: ["documents.search"] }),
  auth, runtime: new Runtime({ directory: ".mcpilot/runtime" }),
});

const toolset = await pilot.toolsFor("Znajdź dokumentację projektu w Notion");
// connection.status === "auth_required" → przycisk już trafił do UI użytkownika

// GET /oauth/mcp/callback (w sesji użytkownika):
await broker.complete({ userId: session.userId, callbackUrl: request.url });   // true / false
const ready = await pilot.toolsFor("Znajdź dokumentację projektu w Notion");
const result = await pilot.call(ready.tools[0].id, { query: "Atlas" }, { signal: AbortSignal.timeout(10_000) });
```

API: `discover`, `plan`, `connect`, `toolsFor`, `call`, `status`, `disconnect`, `close` (także `await using`), oraz `WorkflowRunner`, `DiscoveryTools`, `Catalog.sync()`.

## Zgodność z Pythonem

| Obszar | Gwarancja | Dowód |
| --- | --- | --- |
| Manifesty i odciski | Ten sam JSON (`snake_case`), te same wartości domyślne, odcisk SHA-256 kanonicznego JSON (RFC 8785) | `spec/vectors.json` → `test/vectors.test.ts` |
| Polityka endpointów | Identyczne decyzje, w tym zakresy IANA i przypadki brzegowe parsera URL | 47 wektorów |
| Router | Te same wymagania (PL/EN, granice słów Unicode), wyniki, powody `unavailable` | 12 + 8 wektorów |
| Klucze poświadczeń, ID połączeń i narzędzi | Identyczne | wektory `credentials` |
| Magazyn poświadczeń | `EncryptedFileSecretStore` w formacie Fernet — pliki zapisane przez Python są czytelne w TS i odwrotnie (ten sam klucz) | wektor `fernet` |
| Rejestr | Ta sama normalizacja wpisów (niewykonywalne podsumowania) | wektory `registry` |
| Warstwa discovery, workflow | Identyczne definicje meta-narzędzi i JSON przebiegu | wektory |

Wektory generuje `python scripts/export_vectors.py`; test Pythona pilnuje, żeby plik był aktualny, a testy TS go czytają. Testy e2e TS uruchamiają prawdziwe serwery Python (`tests/runtime_server.py`, fixture OAuth, zdalną bramę) — to test interoperacyjności obu oficjalnych SDK MCP.

## Różnice

- **Logowanie jest nieblokujące.** SDK TS przerywa połączenie po przekierowaniu do autoryzacji (`UnauthorizedError`), więc `connect()` od razu zwraca `auth_required`, a `LoginBroker` przekazuje przycisk do UI. `broker.complete()` wymienia kod (PKCE, `resource`, weryfikacja `iss` przez SDK) i kolejne `connect()` jest gotowe. Weryfikator PKCE trafia do magazynu sekretów.
- **Minimalne scope.** Chroniony `fetch` ogranicza `scopes_supported` w Protected Resource Metadata do scope z polityki, więc SDK nie prosi o wszystkie ogłaszane uprawnienia. `offline_access` (tokeny odświeżania) jest jedynym dopuszczonym dodatkiem; jawne żądanie rozszerzenia od serwera kończy się `auth_required` bez przekierowania.
- **Środowiska uruchomieniowe.** HTTP działa wszędzie, gdzie jest `fetch`. stdio i instalator pakietów ładują moduły Node dynamicznie; magazyny plikowe są w `@mcpilot/sdk/node`.
- **Brak bramy w TS.** Hosty TypeScript łączą się ze zdalną bramą Python jak każdy klient MCP (test `official TypeScript client completes URL elicitation…`).
- **Katalog** synchronizuje się na żądanie (`sync()`); harmonogram należy do hosta.
- **Budżet i audyt** działają jak w Pythonie: limity per zadanie (`ToolSet.task_id`), zdarzenia z `tenant`, `task_id`, `duration_ms` (ten sam zestaw pól, sprawdzany wektorem), `JsonlAuditSink` w `@mcpilot/sdk/node`.

## Status

| Funkcja | Status |
| --- | --- |
| Wektory zgodności z Pythonem | E2E lokalnie |
| stdio i Streamable HTTP z serwerami Python, budżety, brak powtórzeń zapisu, audyt, workflow | E2E lokalnie |
| OAuth z przyciskiem, odmowa obcego użytkownika, ponowne użycie po restarcie | Demonstracyjne (fixture IdP) |
| Klient TS → zdalna brama Python z elicytacją URL | Demonstracyjne |
| Instalacja pakietów npm/PyPI | Walidacja testowana lokalnie; instalacja wymaga sieci |
| Prawdziwe konta dostawców, przeglądarkowe hosty | Wymaga konta / dalszych testów |
