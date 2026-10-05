# Brama MCP

Brama pozwala hostom MCP, które nie importują biblioteki (np. Claude Code, aplikacje desktopowe, IDE, klienci TypeScript), korzystać z MCPilot przez jedno połączenie. Są dwa warianty:

| Wariant | Transport | Użytkownicy | Logowanie u dostawców | Status |
| --- | --- | --- | --- | --- |
| Lokalny (`mcpilot.gateway`) | stdio | jeden, z konfiguracji | przeglądarka otwierana przez proces + callback na loopback | E2E lokalnie |
| Zdalny (`mcpilot.remote`) | Streamable HTTP | wielu, z tokenu hosta; polityka per tenant | elicytacja URL (MCP) prowadząca na stronę bramy | E2E lokalnie / demonstracyjne |

```
host MCP ──stdio / HTTPS + token──▶ brama ──▶ MCPilot (osobny na użytkownika) ──▶ serwery stdio / Streamable HTTP
  model widzi:    mcpilot_find_tools                                       (manifest = zatwierdzone integracje)
                  mcpilot_call_tool
```

Model hosta widzi dwa narzędzia z [warstwy discovery](architecture.md#kontekst-llm). Nie dostaje katalogu, endpointów, poleceń ani poświadczeń i nie może ich podać w argumentach. Narzędzia downstream są wywoływane przez `mcpilot_call_tool` z identyfikatorem zwróconym przez `mcpilot_find_tools`.

# Wariant lokalny (stdio)

## Uruchomienie (zalecane: pakiet pilotażowy)

```sh
pip install './mcpilot-0.1.0-py3-none-any.whl[keychain]'   # wheel z artefaktów CI; z repozytorium: pip install -e '.[keychain]'
python -m mcpilot setup --workspace ~/dokumenty --github-pat-prompt   # Notion + GitHub (+ pliki), Claude Code
python -m mcpilot status                                              # stan połączeń, bez tokenów
python -m mcpilot disconnect notion                                   # usuwa lokalny dostęp
python -m mcpilot uninstall [--purge]                                 # wyrejestrowanie, usunięcie klucza
```

`setup` zapisuje manifest i `gateway.json` w `~/.mcpilot/pilot`, tworzy klucz szyfrujący w pęku kluczy systemu (macOS Keychain, Windows Credential Locker, Secret Service), szyfruje PAT GitHub (z ukrytego pola lub `--github-pat-env`, nigdy z argumentu) i rejestruje bramę w Claude Code (`claude mcp add`, domyślnie `--scope user`) **bez sekretów w konfiguracji hosta**. Istniejącego wpisu o tej samej nazwie nie nadpisuje. Każda sesja hosta uruchamia nowy proces bramy, który odczytuje klucz z pęku kluczy, więc zapisane logowania działają po restarcie.

Ręczna konfiguracja (inne hosty, testy): `python examples/gateway_setup.py` zapisuje `gateway.json` bez klucza; wtedy klucz trzeba podać w `keychain_item` albo zmienną `key_env`, inaczej tokeny żyją tylko w pamięci procesu (brama ostrzega na stderr).

### Konfiguracja `gateway.json`

| Pole | Znaczenie | Domyślnie |
| --- | --- | --- |
| `user_id` | Użytkownik, w imieniu którego działa proces | wymagane |
| `manifest` | Plik zatwierdzonych integracji administratora (ścieżka względem pliku konfiguracji) | wymagane |
| `capabilities` | Capabilities dopuszczone polityką | wymagane |
| `effects` | Dopuszczone skutki narzędzi | `["read", "draft"]` |
| `accounts` | Dopuszczone etykiety kont | `["default"]` |
| `state_dir` | Cache katalogu, środowiska pakietów, poświadczenia | `~/.mcpilot` |
| `allow_loopback` | Zezwolenie na endpointy `127.0.0.1` (testy) | `false` |
| `key_env` | Zmienna z kluczem Fernet (ma pierwszeństwo przed pękiem kluczy) | `MCPILOT_SECRET_KEY` |
| `keychain_item` | Wpis w pęku kluczy systemu (usługa `mcpilot`) z kluczem Fernet; ustawia go `setup` | brak |
| `login_port` | Port callbacku OAuth na `127.0.0.1` (część `redirect_uri`) | `8765` |
| `connect_wait` | Sekundy oczekiwania na połączenie, zanim wynik zgłosi `auth_pending` | `15` |
| `secrets` | Mapa `id integracji → nazwa zmiennej` z PAT/kluczem (alternatywa dla PAT zapisanego przez `setup`; zmienna trafia wtedy do konfiguracji hosta) | `{}` |
| `audit_log` | Plik JSON Lines ze zdarzeniami audytu (0600; bez argumentów, wyników i sekretów); wejście dla `python -m mcpilot.metrics` | brak |
| `registry_sync` | Odświeżanie MCP Registry w tle i przebudowa indeksu wyszukiwania (sugestie `needs_admin_approval`); `setup` włącza domyślnie | `false` |
| `registry_sync_interval` | Odstęp synchronizacji w sekundach | `3600` |

Nieznane pola są odrzucane. Każda integracja z manifestu jest zatwierdzona dokładnie w tej wersji. Brama lokalna sprawdza czas modyfikacji `gateway.json` i manifestu przed każdym `mcpilot_find_tools` i przeładowuje zatwierdzenia bez restartu (np. po `python -m mcpilot approve`, [approval.md](approval.md)); błędny plik zostawia poprzednie zatwierdzenia. Brama zdalna wymaga restartu.

## Konfiguracja hosta

Host musi raz dodać bramę jako serwer stdio. Biblioteka nie dopisuje się sama do konfiguracji żadnego hosta.

**Claude Code** (robi to `python -m mcpilot setup`):

```sh
claude mcp add mcpilot -- /ścieżka/.venv/bin/python -m mcpilot.gateway --config ~/.mcpilot/pilot/gateway.json
```

**Hosty z plikiem `mcpServers`:**

```json
{
  "mcpServers": {
    "mcpilot": {
      "command": "/ścieżka/.venv/bin/python",
      "args": ["-m", "mcpilot.gateway", "--config", "/ścieżka/.mcpilot/pilot/gateway.json"]
    }
  }
}
```

`gateway_setup.py` wypisuje wpisy dla Claude Code, Cursor i VS Code z poprawnymi ścieżkami. Który host jest zweryfikowany, a który tylko zadeklarowany w dokumentacji: [hosts.md](hosts.md). Wymagania po stronie hosta:

- interpreter z zainstalowanym `mcpilot` (bezwzględna ścieżka do venv);
- klucz szyfrujący w pęku kluczy systemu (`setup`, pole `keychain_item`), jeśli tokeny mają przetrwać restart; nie umieszczaj klucza ani PAT w sekcji `env` konfiguracji hosta, bo trafiają tam otwartym tekstem;
- możliwość otwarcia przeglądarki i wolny port `login_port` na loopback dla logowania OAuth;
- limit czasu narzędzi hosta większy niż `connect_wait` (wywołanie wraca po tym czasie z `auth_pending`, a logowanie kończy się w tle);
- ewentualne `npm`/Python dla integracji instalowanych lokalnie.

## Logowanie przez bramę

Gdy integracja wymaga OAuth, brama otwiera przeglądarkę na stronie dostawcy i nasłuchuje przekierowania na `http://127.0.0.1:<login_port>/callback`. Model dostaje tylko `needs_user: [{"action": "finish_login", ...}]` i powinien poprosić użytkownika o dokończenie logowania, a potem ponownie wywołać `mcpilot_find_tools`. URL logowania trafia do przeglądarki, nie do wyniku narzędzia ani logów stdio. Brak poświadczeń dla integracji `bearer` daje `connect_account`; brak konfiguracji aplikacji daje `admin_setup`.

## Granice bezpieczeństwa (lokalnie)

- Jeden proces bramy obsługuje jednego użytkownika. Autoryzacja host → brama to granica procesu lokalnego (stdio), oddzielna od autoryzacji brama → serwer dostawcy.
- Wyniki downstream nie zmieniają konfiguracji bramy, polityki ani manifestu.
- Polecenia i adresy pochodzą wyłącznie z manifestu administratora; argumenty modelu nie mogą ich wskazać.
- Utrata połączenia hosta zamyka bramę i jej sesje; trwały jest magazyn poświadczeń i cache katalogu.

# Wariant zdalny (Streamable HTTP, wielu użytkowników)

```sh
export MCPILOT_SECRET_KEY=<klucz Fernet>          # poświadczenia dostawców, wymagany
export MCPILOT_REQUEST_STATE_KEY=<losowy sekret>  # wspólny dla instancji; bez niego klucz efemeryczny
python -m mcpilot.gateway --config examples/remote_gateway.json
python -m examples.remote_gateway_demo            # demonstracja na loopback
```

## Autoryzacja host → brama

Brama jest serwerem zasobów OAuth zgodnym z autoryzacją MCP. Publikuje Protected Resource Metadata (`/.well-known/oauth-protected-resource/mcp`) wskazujące IdP organizacji, a żądania bez ważnego tokenu dostają `401` z `WWW-Authenticate: Bearer resource_metadata=…`. Host MCP przeprowadza OAuth u IdP i wysyła token przy każdym żądaniu.

| Element | Implementacja |
| --- | --- |
| Weryfikacja tokenu | `JWTTokenVerifier`: podpis (JWKS URL lub statyczny zestaw kluczy), `iss`, `aud` = `https://brama/mcp`, `exp`, `sub`; `StaticTokenVerifier` tylko do testów; dowolny `TokenVerifier` z SDK MCP |
| Użytkownik | `issuer\|sub` (domyślnie) albo wskazany claim (`user_claim`, np. `email`) |
| Tenant | claim `tenant_claim` (np. `org_id`); nieznany tenant → `PolicyDenied` |
| Wymagane scope | `required_scopes`, np. `["mcpilot"]` |

## Izolacja użytkowników i tenantów

- Każdy użytkownik dostaje własny obiekt `MCPilot`: własne sesje, identyfikatory narzędzi, katalog procesów (`state_dir/users/<hash>`) i limity. Identyfikator narzędzia innego użytkownika jest odrzucany (`PolicyDenied`).
- Każdy tenant ma własny manifest i politykę. Zmiana tenanta w tokenie zamyka sesję użytkownika.
- Przypięte pakiety są współdzielone między użytkownikami, procesy nie.
- Poświadczenia dostawców leżą we wspólnym zaszyfrowanym magazynie, ale klucz zawiera użytkownika, integrację, konto i endpoint.
- Limity: `max_users` (wypieranie najdłużej bezczynnych), `idle_timeout` (zamykanie sesji i procesów), `calls_per_minute` (token bucket na użytkownika, wynik `RateLimited`).
- `requestState` elicytacji jest szyfrowany (AES-GCM) i wiązany z uwierzytelnionym użytkownikiem przez SDK MCP, więc nie da się go odtworzyć w cudzej sesji.

## Logowanie przez elicytację URL

```
model ─ mcpilot_find_tools ─▶ brama: Notion wymaga OAuth → LoginBroker → link /connect/<id>
host  ◀─ InputRequiredResult{ elicitation/create, mode: url, url: https://brama/connect/<id> } (2026-07-28)
       (starsi klienci: błąd -32042 URLElicitationRequired z elicitationId)
host pokazuje prośbę „Połącz konto”, użytkownik akceptuje ─▶ klient ponawia wywołanie z odpowiedzią
przeglądarka ─▶ /connect/<id>: tożsamość przeglądarki == właściciel linku? → ciasteczko → 302 do dostawcy
dostawca ─▶ /oauth/callback?code&state: ciasteczko + state → wymiana kodu (PKCE) → token zaszyfrowany
brama (ponowione wywołanie czeka do login_wait) ─▶ wynik z narzędziami; model nie widział URL
```

1. Link prowadzi na stronę bramy, nie bezpośrednio do dostawcy. Strona sprawdza tożsamość przeglądarki (`browser_identity`) i tylko właściciel linku zostaje przekierowany do dostawcy. Przekazany komuś link daje `403`, więc nie da się podpiąć cudzego konta do inicjującego użytkownika.
2. Callback jest przyjmowany tylko z ciasteczkiem ustawionym przez stronę `/connect` w tej samej przeglądarce i ze zgodnym `state`; link jest jednorazowy i wygasa po `login_timeout`.
3. Klient bez obsługi elicytacji URL dostaje zwykły wynik z `needs_user: finish_login` i statycznym adresem `connect_page` (lista oczekujących połączeń dla zalogowanej przeglądarki). Adres nie zawiera sekretów.
4. Logowanie trwające dłużej niż `login_wait` kończy ponowione wywołanie wynikiem `finish_login`; połączenie kończy się w tle i następne `mcpilot_find_tools` zwraca narzędzia.

`browser_identity` dostarcza wdrożenie. Typowo brama stoi za proxy tożsamości organizacji (np. oauth2-proxy, IAP), które uwierzytelnia przeglądarkę i wstrzykuje nagłówek; `browser_identity_header` włącza `trusted_header_identity` — **wyłącznie** za takim proxy, które usuwa nagłówek z żądań klientów. Wartość musi odpowiadać identyfikatorowi użytkownika z tokenu (np. `user_claim: "email"` i nagłówek z e-mailem). Bez `browser_identity` logowanie przez URL jest wyłączone, chyba że deweloper jawnie ustawi `trust_connect_links=True` (tylko do testów lokalnych).

## Konfiguracja `"transport": "http"`

| Pole | Znaczenie |
| --- | --- |
| `public_url` | Origin bramy (HTTPS; HTTP tylko na loopback). Wyznacza `redirect_uri` dostawców: `<public_url>/oauth/callback` |
| `listen` | `host`, `port` serwera ASGI (za reverse proxy z TLS) |
| `auth` | `issuer`, `audience`, `jwks_url` albo `jwks`, `required_scopes`, `tenant_claim`, `user_claim` |
| `tenants` | `nazwa → {manifest, capabilities, effects, accounts}` |
| `state_dir` | Procesy użytkowników, pakiety, poświadczenia |
| `key_env` | Zmienna z kluczem Fernet (wymagana) |
| `request_state_keys_env` | Zmienna z kluczem `requestState` wspólnym dla instancji |
| `browser_identity_header` | Nagłówek tożsamości z proxy |
| `audit_log` | Dziennik audytu JSON Lines; zdarzenia zawierają tenant, użytkownika, `task_id` i `duration_ms` |
| `max_users`, `idle_timeout`, `calls_per_minute`, `connect_wait`, `login_wait`, `login_timeout` | Limity i czasy |

Przykład: [examples/remote_gateway.json](../examples/remote_gateway.json). Nieznane pola są odrzucane. Klucze dostawców OAuth bez DCR (np. Slack) konfiguruje się w kodzie przez `AuthManager.configure_oauth` z `redirect_uri` bramy.

## Wymagania hosta (wariant zdalny)

- obsługa autoryzacji MCP (OAuth u IdP organizacji wskazanego w Protected Resource Metadata) albo przekazanie tokenu przez aplikację;
- dla logowania jednym kliknięciem: obsługa elicytacji w trybie URL (`capabilities.elicitation.url`); bez niej użytkownik korzysta z `connect_page`;
- limit czasu wywołań narzędzi co najmniej `login_wait` + kilka sekund.

## Wdrożenie

- TLS i nagłówki tożsamości na reverse proxy. Brama sprawdza `Host` (ochrona przed DNS rebinding) względem `public_url`, więc proxy musi przekazywać oryginalny nagłówek `Host`.
- Jedna instancja: klucz `requestState` może być efemeryczny. Wiele instancji: wspólny klucz i przyklejone sesje (oczekujące logowania i procesy stdio są w pamięci instancji) albo jedna instancja na użytkownika.
- Produkcyjny magazyn poświadczeń wielu instancji: własny `SecretStore` (KMS/Vault) zamiast plików.
- Audyt: `audit=` w `RemoteGateway` przekazuje zdarzenia każdego użytkownika (bez argumentów i wyników).

## Ograniczenia i kolejne etapy

- Oczekujące logowania (weryfikator PKCE) i sesje stdio są w pamięci procesu; restart wymaga ponownego kliknięcia.
- Brak powiadomienia o zakończeniu elicytacji (2026-07-28 usunął `elicitationId`); klient ponawia wywołanie, a brama czeka do `login_wait`.
- Opcjonalne wystawianie wybranych narzędzi downstream jako narzędzi pierwszego poziomu (`tools/list_changed`) przy zachowaniu budżetu kontekstu.
- Testy na prawdziwym IdP organizacji i kontach dostawców: wymagają konta.
