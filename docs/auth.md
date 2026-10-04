# Autoryzacja i logowanie

Logowanie odbywa się u dostawcy. Hasła, tokeny i klucze nie trafiają do modelu, wyjątków, logów ani zdarzeń audytu. Model widzi identyfikator połączenia, status i przyznane capabilities. URL logowania otrzymuje wyłącznie interfejs hosta.

## Dwa poziomy dostępu

| Poziom | Kto autoryzuje | Co obsługuje MCPilot |
| --- | --- | --- |
| Klient → serwer MCP | Użytkownik loguje się u dostawcy (OAuth) albo host przekazuje PAT lub klucz API | Discovery, rejestracja klienta, PKCE, przechowywanie, odświeżanie, cofanie |
| Serwer MCP → usługa docelowa | Serwer dostawcy, np. Notion MCP działa w ramach dostępu użytkownika do workspace | Nic: MCPilot tego nie widzi ani nie rozszerza. Opisuje to `AuthSpec.target_service_auth`. |

Token jest wiązany z zasobem (`resource`, RFC 8707) i kluczem `(użytkownik, integracja, konto, endpoint)`. Nie jest przekazywany innemu serwerowi.

## Adaptery

| `AuthSpec.mode` | Zastosowanie | Przekazanie |
| --- | --- | --- |
| `none` | Lokalny serwer bez poświadczeń | — |
| `oauth` | Serwery zdalne zgodne z autoryzacją MCP (np. Notion) | `httpx2.Auth` z oficjalnego SDK MCP |
| `bearer` | PAT lub token wydany poza MCPilot (np. GitHub PAT) | `Authorization: Bearer …` |
| `api_key` | Klucz dostawcy | Nagłówek `header_name` albo, dla stdio, zmienna `env_var` |

OAuth realizuje [specyfikację autoryzacji MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization) przez oficjalne SDK `mcp==2.3.0`: Protected Resource Metadata (RFC 9728), metadane serwera autoryzacji (RFC 8414 / OpenID), rejestracja klienta (wstępnie zarejestrowany klient powiązany z `issuer`, Client ID Metadata Document przez `client_metadata_url` albo DCR jako zgodność wstecz), PKCE S256, `state` i weryfikacja `iss` odpowiedzi. MCPilot dodaje do tego minimalne scope, kontrolę hostów autoryzacji, redakcję logów i odporny na restart magazyn.

## Przepływ logowania (przycisk)

```
pętla agenta / workflow      MCPilot                         serwer MCP     dostawca (IdP)    UI hosta
tools_for(plan) ──────────▶ prepare(): brak tokenu
                            sonda POST (ping) ─────────────▶ 401 + WWW-Authenticate
                            PRM, metadane AS, rejestracja klienta ───────────▶
                            LoginBroker.begin() ─────────────────────────────────────────────▶ przycisk „Połącz”
                                                                             ◀── klik, zgoda użytkownika
                            ◀── GET /callback?code&state&iss (trasa hosta, sesja użytkownika)
                            broker.complete_url() → wymiana kodu (PKCE, resource) ──▶ token
                            zapis zaszyfrowany; initialize + tools/list ──▶ ready
◀── ToolSet / workflow kontynuuje od niewykonanego kroku
```

1. `AuthManager.prepare()` sprawdza magazyn. Bez tokenu i bez UI logowania zwraca `auth_required` natychmiast, bez ruchu sieciowego.
2. Faza autoryzacji (`AuthHandle.authorize`) wysyła sondę JSON-RPC `ping` przed otwarciem sesji MCP. Logowanie może trwać minuty, a inicjalizacja MCP ma krótkie timeouty, więc obie fazy są rozdzielone. Limit fazy to `OAuthConfig.authorization_timeout` plus `LoginBroker.timeout`.
3. `LoginBroker.begin()` przekazuje `AuthorizationRequest` (połączenie, integracja, konto, scope, URL, użytkownik) do funkcji `notify` hosta.
4. Trasa callback hosta, działająca w sesji zalogowanego użytkownika, wywołuje `complete_url()`. Broker akceptuje tylko znany `state` tego samego użytkownika; odmowa (`error=access_denied`) daje `auth_required`.
5. Brak kliknięcia w czasie `timeout` daje `auth_required`. Workflow zapisuje `waiting_for_auth` i może zostać wznowiony później.

### Integracja hosta webowego

```python
from mcpilot.auth import AuthManager, AuthorizationRequest, EncryptedFileSecretStore, LoginBroker, OAuthConfig

async def show_connect_button(request: AuthorizationRequest) -> None:
    # Wyślij do przeglądarki tego użytkownika (np. WebSocket). Nigdy do modelu ani logów.
    await ui.push(request.user_id, {
        "type": "connect_account", "integration": request.integration_id,
        "connection_id": request.connection_id, "url": request.url,
    })

broker = LoginBroker(show_connect_button, timeout=600)
auth = AuthManager(
    EncryptedFileSecretStore(".mcpilot/credentials", key_from_secret_manager),
    OAuthConfig(login=broker, redirect_uri="https://app.example.com/oauth/mcp/callback"),
)

# GET /oauth/mcp/callback, za uwierzytelnieniem sesji aplikacji
async def oauth_callback(http_request):
    user = current_user(http_request)
    if broker.complete_url(user_id=user.id, callback_url=str(http_request.url)):
        return redirect("/tasks")
    return error_page("Logowanie wygasło lub należy do innego użytkownika")
```

`broker.pending(user_id)` odtwarza przyciski po przeładowaniu strony. `broker.cancel(user_id, connection_id)` przerywa oczekiwanie. `complete()` można wywołać z innego wątku. Pełny działający przykład: [examples/notion_github_task.py](../examples/notion_github_task.py).

Hosty desktopowe i brama MCP używają `LoopbackLogin`: otwiera przeglądarkę i odbiera przekierowanie na `http://127.0.0.1:<port>/callback`.

Alternatywnie host może podać parę `authorization_handler`/`redirect_handler` i `callback_handler`, jeśli sam zarządza oczekiwaniem.

### Elicytacja URL (brama zdalna)

Gdy host łączy się z MCPilot przez [zdalną bramę](gateway.md#wariant-zdalny-streamable-http-wielu-użytkowników), przycisk logowania dostarcza sam protokół MCP. `mcpilot_find_tools` zwraca `InputRequiredResult` z żądaniem `elicitation/create` w trybie `url` (protokół 2026-07-28; starsi klienci dostają błąd `-32042` z `elicitationId`). URL prowadzi na stronę bramy `/connect/<id>`, która sprawdza tożsamość przeglądarki, ustawia ciasteczko wiążące callback z tą przeglądarką i dopiero wtedy przekierowuje do dostawcy. Model nie widzi URL, a przekazany link nie pozwoli podpiąć cudzego konta.

### TypeScript

W [SDK TypeScript](typescript.md) logowanie jest nieblokujące: `connect()` zwraca `auth_required` od razu po wysłaniu przycisku, a `broker.complete({ userId, callbackUrl })` wymienia kod i zapisuje token. Weryfikator PKCE jest przechowywany w magazynie sekretów. Scope ogłaszane przez serwer są przycinane do polityki; `offline_access` jest jedynym dopuszczonym dodatkiem.

## Minimalne scope i rozszerzanie dostępu

`AuthSpec.scopes_by_capability` wyznacza scope tylko dla capabilities z bieżącego planu. Ten sam zbiór trafia do rejestracji klienta i żądania autoryzacji, zamiast wszystkich scope ogłaszanych przez serwer. Token bez żądanych scope nie jest uznawany za gotowy. Wyzwanie serwera (`WWW-Authenticate: … scope=…`) spoza polityki kończy się `auth_required` z informacją o potrzebie zmiany polityki, bez otwierania przeglądarki. Rozszerzenie dostępu to zmiana polityki hosta i nowa zgoda użytkownika; działania w obrębie już przyznanych scope nie wymagają ponownych potwierdzeń.

## Odświeżanie, restart, cofnięcie

- Tokeny, rejestracja klienta, termin wygaśnięcia i odkryty token endpoint są zapisywane, więc po restarcie SDK odświeża token bez nowego logowania. Do ponownego użycia zapisanego tokenu nie jest potrzebny skonfigurowany UI logowania.
- `MCPilot.disconnect(id, account=..., revoke=True)` zamyka sesję i usuwa lokalne poświadczenia. `AuthManager.revoke(..., provider_revoker=...)` pozwala hostowi dodać unieważnienie u dostawcy. Kopii tokenu przechowywanej poza MCPilot nie da się cofnąć lokalnie.
- Błędy odświeżania lub odrzucony token prowadzą do `auth_required`, a nie do ujawnienia odpowiedzi serwera.

## Wiele kont i izolacja użytkowników

Konto jest etykietą hosta (`"default"`, `"work"`). `TaskRequest(accounts={"github": "work"})` wiąże usługę z kontem, a `Policy(accounts=...)` ogranicza dopuszczone etykiety. Klucz poświadczeń zawiera użytkownika, integrację, konto i endpoint, dlatego token jednego użytkownika, konta lub serwera nie zostanie użyty dla innego. Skrót klucza to SHA-256 kanonicznego JSON (RFC 8785), identyczny w Pythonie i TypeScript, więc oba SDK mogą współdzielić magazyn sekretów (np. ten sam katalog `EncryptedFileSecretStore` i klucz Fernet). Jeden obiekt `MCPilot` należy do jednego użytkownika uwierzytelnionego przez hosta.

## Magazyny sekretów

| Magazyn | Zastosowanie |
| --- | --- |
| `MemorySecretStore` | Testy i krótkie sesje. |
| `EncryptedFileSecretStore(dir, key)` | Jeden proces aplikacji: Fernet, pliki 0600, zapis atomowy. Klucz pochodzi z menedżera sekretów hosta. |
| Własny `SecretStore` | Trzy metody async (`get`, `set`, `delete`) nad KMS, Vault lub bazą aplikacji, zalecane dla wielu instancji. |

## `setup_required`

Status oznacza, że brak zgody użytkownika nie jest problemem: potrzebna jest konfiguracja aplikacji lub decyzja administratora. Przykłady (szczegóły w [integrations.md](integrations.md)):

- **Slack:** brak DCR; wymagana zarejestrowana, dopuszczona aplikacja (`OAuthConfig(client_id=..., client_secret=..., issuer=...)`).
- **Google Drive:** program Developer Preview, projekt Google Cloud, włączone API, klient OAuth.
- **Wstępnie zarejestrowany klient bez `issuer`** albo `client_secret` bez `client_id`.
- Brak trybu auth w adapterze lub brak zmiennej środowiskowej dla poświadczenia stdio.

Host pokazuje wtedy instrukcję dla administratora zamiast przycisku logowania. W planie integracje bez skonfigurowanego adaptera pojawiają się w `Plan.unavailable` z kodem `setup_required`.
