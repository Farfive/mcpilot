# Zweryfikowane integracje

Stan dokumentacji: **2026-10-04**. Weryfikacja oznacza sprawdzenie źródeł dostawcy, a nie wykonanie testu na koncie użytkownika. Nazwy capabilities w MCPilot są warstwą routingu; schematy i faktyczne nazwy narzędzi należy pobierać przez `tools/list` po autoryzacji. Endpoint z dokumentacji nie jest automatycznie zatwierdzonym endpointem organizacji.

| Kandydat | Pochodzenie | Transport i adres/pakiet | Zalecany zakres pierwszego wdrożenia |
| --- | --- | --- | --- |
| GitHub | Oficjalny, GitHub | Streamable HTTP: `https://api.githubcopilot.com/mcp/` | Wyszukiwanie i odczyt issue oraz repozytoriów; konto wymagane |
| Notion | Oficjalny, Notion | Streamable HTTP: `https://mcp.notion.com/mcp` | Wyszukiwanie i odczyt dokumentacji; konto i interaktywne OAuth wymagane |
| Google Drive | Oficjalny, Google; Developer Preview | Streamable HTTP: `https://drivemcp.googleapis.com/mcp/v1` | Odkrywanie w katalogu; `setup_required` do konfiguracji projektu i dostępu do preview |
| Slack | Oficjalny, Slack | Streamable HTTP: `https://mcp.slack.com/mcp` | Odkrywanie w katalogu; `setup_required` do konfiguracji dopuszczonej aplikacji |
| Filesystem | Oficjalny serwer referencyjny projektu MCP | stdio; npm `@modelcontextprotocol/server-filesystem@2026.8.31` | Odczyt tylko jawnie dopuszczonych katalogów; osobno od lokalnego serwera demonstracyjnego MCPilot |

Źródła adresów: [GitHub](https://github.com/github/github-mcp-server), [Notion](https://developers.notion.com/guides/mcp/get-started-with-mcp), [Google Drive](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server), [Slack](https://docs.slack.dev/ai/slack-mcp-server/), [filesystem](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem). Wersję publikowanego pakietu filesystem potwierdza [historia wydań npm](https://www.npmjs.com/package/%40modelcontextprotocol/server-filesystem?activeTab=versions); wersja w roboczym `package.json` repozytorium nie zastępuje wersji wydania.

## GitHub

Serwer zdalny nie potrzebuje lokalnego runtime. Dostawca dokumentuje OAuth oraz PAT w nagłówku `Authorization: Bearer`. Własny host obsługujący OAuth musi skonfigurować GitHub App lub OAuth App. Przykłady logowania jednym kliknięciem w IDE nie oznaczają, że dowolna nowa aplikacja ma gotową rejestrację klienta. Obowiązują polityki organizacji i uprawnienia tokenu. Źródło: [oficjalne repozytorium GitHub MCP](https://github.com/github/github-mcp-server).

Dostępny jest endpoint `https://api.githubcopilot.com/mcp/readonly` oraz zestawy narzędzi, np. `https://api.githubcopilot.com/mcp/x/issues/readonly`. To użyteczne ograniczenie po stronie serwera, ale nie zastępuje kontroli uprawnień w MCPilot. Źródło: [konfiguracja zdalnego serwera](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md).

Weryfikacja 2026-10-05 (E2E sieć, bez konta): oficjalna binarka `github-mcp-server` v1.14.0 w trybie `stdio --read-only` zwraca 27 narzędzi, w tym `search_issues`, `issue_read` i `list_issues` z szablonu. `issue_read` wymaga `method` (`get`, `get_comments`, `get_sub_issues`, `get_parent`, `get_labels`). `github-mcp-server list-scopes` podaje dla narzędzi issue klasyczny scope `repo`. Migawka: `spec/providers/github-mcp-server-1.14.0.json`. Serwer hostowany może różnić się zestawem narzędzi; potwierdza to dopiero przebieg na koncie (`python -m mcpilot.pilot`).

Decyzja MVP: preferować odczyt; PAT przyjmować wyłącznie przez magazyn sekretów hosta (`AuthManager.set_secret`) lub zmienną środowiskową bramy. Szablon `github(endpoint=...)` domyślnie wskazuje `/mcp/readonly`. OAuth bez zarejestrowanego klienta zwraca `setup_required`. Nie kodować uniwersalnego szerokiego scope `repo` jako domyślnego zakresu każdego zadania; użyć uprawnień i wyzwania autoryzacyjnego właściwych dla konkretnego konta i operacji.

## Notion

Hostowany serwer wymaga interaktywnej autoryzacji OAuth i korzysta z dostępu użytkownika w wybranym workspace. Notion dokumentuje także starszy endpoint SSE, ale MVP korzysta ze Streamable HTTP. Sam token integracji Notion API nie zastępuje autoryzacji hostowanego MCP. Lokalny `notion-mcp-server` jest osobnym, obecnie nieaktywnie utrzymywanym rozwiązaniem. Źródło: [podłączenie Notion MCP](https://developers.notion.com/guides/mcp/get-started-with-mcp).

Dokumentacja budowy klienta opisuje discovery metadanych, dynamiczną rejestrację klienta, PKCE S256 oraz odświeżanie tokenów. Należy odczytywać endpointy autoryzacji z metadanych zamiast konstruować je z nazwy usługi. Źródło: [własny klient Notion MCP](https://developers.notion.com/guides/mcp/build-mcp-client).

Weryfikacja 2026-10-05 (dokumentacja, bez konta): [lista narzędzi Notion MCP](https://developers.notion.com/guides/mcp/mcp-supported-tools) zawiera `notion-search` (wymagane `query`) i `notion-fetch` (URL lub ID) jako operacje tylko do odczytu. Dokładną nazwę parametru `notion-fetch` potwierdzi `tools/list` na koncie. Migawka: `spec/providers/notion-mcp-docs-2026-10-05.json`.

Decyzja MVP: główny kandydat do ręcznego testu OAuth na rzeczywistym koncie. Biblioteka zwraca hostowi akcję logowania; model otrzymuje status i identyfikator połączenia. Workflow przechowuje plan, po zalogowaniu ponownie odkrywa narzędzia i kontynuuje od niewykonanego kroku. Bez konta test OAuth na lokalnym serwerze pozostaje demonstracją mechanizmu, nie testem Notion.

## Google Drive

Oficjalny serwer istnieje w programie Google Workspace Developer Preview. Wymaga członkostwa w programie, projektu Google Cloud, włączenia `drive.googleapis.com` i `drivemcp.googleapis.com`, ekranu zgody OAuth oraz klienta OAuth. Dokumentowane scopes to `https://www.googleapis.com/auth/drive.readonly` i `https://www.googleapis.com/auth/drive.file`. Przykłady hostów wymagają klienta i sekretu z właściwym redirect URI. Źródło: [konfiguracja Drive MCP](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server).

Dostęp do pliku podlega dodatkowo regułom kwalifikacji: ACL, IRM/DLP, Context Aware Access i szyfrowaniu po stronie klienta. To, że użytkownik widzi plik w aplikacji, nie gwarantuje możliwości odczytu przez MCP. Źródło: [kwalifikacja plików](https://developers.google.com/workspace/drive/api/guides/drive-mcp-server-file-eligibility).

Google wymaga filtrowania promptów i odpowiedzi pod kątem złośliwych treści; opisuje Model Armor albo udokumentowane własne rozwiązanie. Sam filtr narzędzi MCPilot nie daje podstaw do deklarowania spełnienia tego warunku. Źródło: [bezpieczeństwo Workspace MCP](https://developers.google.com/workspace/guides/configure-mcp-security).

Decyzja MVP: manifest odkryty, bez obietnicy gotowego połączenia. `setup_required` wskazuje brakujące warunki. Dla pilota odczytu proponujemy najmniejszy dopuszczalny scope, następnie sprawdzenie rzeczywistego challenge i dostępnych narzędzi; nie deklarujemy, że sam `drive.readonly` wystarczy dla wszystkich operacji serwera.

## Slack

Slack udostępnia Streamable HTTP; nie obsługuje starszego transportu SSE ani DCR. Klient musi mieć stały identyfikator zarejestrowanej aplikacji Slack. Dopuszczone są aplikacje wewnętrzne lub opublikowane w Marketplace; aplikacje unlisted są wykluczone. Zastosowanie mają zatwierdzanie aplikacji przez administratorów oraz skonfigurowane allowlisty IP. Źródło: [oficjalny Slack MCP](https://docs.slack.dev/ai/slack-mcp-server/).

OAuth dla tokenu użytkownika używa `https://slack.com/oauth/v2_user/authorize` i `https://slack.com/api/oauth.v2.user.access`. Metadane są pod `https://mcp.slack.com/.well-known/oauth-protected-resource` i `https://mcp.slack.com/.well-known/oauth-authorization-server`. Standardowy klient confidential potrzebuje `client_id` i `client_secret`. Zakresy wyszukiwania zależą od rodzaju konwersacji; wysyłanie wymaga `chat:write`. Źródło: [autoryzacja i scopes Slack MCP](https://docs.slack.dev/ai/slack-mcp-server/#authentication-and-token-handling).

Klient desktop może korzystać z PKCE po włączeniu tej opcji w aplikacji. Włączenie zmienia aplikację w klienta publicznego i nie jest samodzielnie odwracalne; refresh tokeny mają wtedy 30-dniowy termin ważności. Źródło: [PKCE w Slack](https://docs.slack.dev/authentication/using-pkce/).

Decyzja MVP: `setup_required`, dopóki host nie skonfiguruje dopuszczonej aplikacji. Przygotowanie treści wiadomości pozostaje lokalnym wynikiem hosta. Wysłanie jest osobną operacją wymagającą autoryzacji do konkretnego odbiorcy; samo polecenie „przygotuj podsumowanie” jej nie przyznaje.

## Lokalny filesystem

Oficjalny serwer referencyjny MCP jest procesem Node.js używającym stdio. Obsługuje m.in. odczyt, zapis i wyszukiwanie. Katalogi określa się argumentami procesu albo przez MCP roots; roots przekazane przez klienta zastępują listę argumentów. Brak dopuszczonych katalogów uniemożliwia inicjalizację. Źródła: [README filesystem](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem), [implementacja transportu](https://github.com/modelcontextprotocol/servers/blob/main/src/filesystem/index.ts).

Decyzja MVP: przypięta wersja i jawna lista katalogów. Szablon `reference_filesystem()` mapuje wyłącznie narzędzia odczytu; 2026-10-04 test sieciowy potwierdził instalację `2026.8.31` przez npm z `--ignore-scripts`, `tools/list` i odczyt pliku, przy ukrytych narzędziach zapisu (`tests/test_network.py`). Użycie odrębnego katalogu instalacji lub venv izoluje zależności, lecz nie stanowi sandboxa systemu operacyjnego. Dla pakietów wymagających silnej izolacji host powinien dostarczyć kontener z ograniczonymi mountami, użytkownikiem i siecią. Dopuszczenie manifestu do katalogu nie upoważnia do uruchomienia pakietu przy imporcie biblioteki. Lokalny serwer demonstracyjny w repozytorium jest autorskim fixture MCPilot, a nie oficjalnym pakietem filesystem.

## Katalog i synchronizacja

Oficjalny katalog udostępnia `GET https://registry.modelcontextprotocol.io/v0.1/servers`, filtr `version=latest`, paginację przez `metadata.nextCursor` oraz statusy w `_meta`. Rejestry prywatne mogą implementować ten sam kontrakt. Źródło: [Registry API](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/generic-registry-api.md), [schemat OpenAPI](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/openapi.yaml).

`Catalog.sync()` najpierw pobiera pełny, ograniczony liczbą stron snapshot każdego źródła, a potem zmiany przez `updated_since` (z zakładką), z pełnym odświeżeniem co 24 h. Wynik zastępuje wcześniejszy stan dopiero po poprawnym odebraniu wszystkich stron; błąd zachowuje działający cache i zapisuje kod błędu. 2026-10-04 oficjalny rejestr zawierał 40 209 wpisów (pełna synchronizacja 76 s, przyrostowa 1 s), w tym `com.notion/mcp` pod tym samym ID co szablon MCPilot. `start_sync()` uruchamia jawną pętlę w tle, a `close()` ją zamyka. Konstruktor może odczytać wskazany lokalny cache, import nie wykonuje I/O. Szczegóły: [catalog.md](catalog.md).

`get()` i `all()` zwracają wyłącznie manifesty dodane przez zaufany kod hosta lub `load_manifest()`. Dane z sieci trafiają tylko do `discovered(limit=20)` jako ograniczone, niezaufane opisy bez poleceń uruchomienia i adresów MCP. Nie nadpisują zaakceptowanej integracji o tym samym ID. Pole `publisher_namespace` opisuje namespace wpisu rejestru, nie dodatkową certyfikację wydawcy. Host musi świadomie przygotować manifest i reguły narzędzi, zanim nowa integracja będzie wykonywalna.

## Status wsparcia i weryfikacja

Rozdzielamy pochodzenie serwera od wyników integracji. „Oficjalny” oznacza wydawcę, a nie zgodność z każdą wersją klienta. Serwery community mogą być dodawane przez osobne manifesty z własnym wydawcą, rewizją i recenzją; nie zastępują po cichu wskazanej przez użytkownika integracji. Katalog nie deklaruje pełnego pokrycia MCP.

Stan `discovered` oznacza obecność metadanych, `supported` obsługiwany adapter, `approved` dopuszczenie przez politykę, `connected` ustanowioną sesję, a `ready` potwierdzone narzędzia i uprawnienia. Brak konfiguracji aplikacji lub zgody administratora to `setup_required`, a brak zgody użytkownika to akcja podłączenia konta. To różne przyczyny i różne instrukcje dla hosta.

Testy akceptacyjne dla prawdziwych kont powinny zapisać datę, dostawcę, transport, metodę autoryzacji, wykonany odczyt, ponowne użycie połączenia oraz wynik cofnięcia dostępu, bez tokenów i treści prywatnych. Lokalne fixture HTTP/stdio/OAuth sprawdzają implementację protokołu i workflow. Nie potwierdzają dostępu produkcyjnego do żadnego z czterech SaaS.

Punktem odniesienia dla nowych adapterów auth jest [specyfikacja MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization): Protected Resource Metadata RFC 9728, metadata serwera autoryzacji, PKCE, identyfikacja zasobu i weryfikacja issuer odpowiedzi. DCR pozostaje mechanizmem zgodności wstecznej. Dostęp klienta do MCP oraz dostęp serwera do usługi docelowej należy modelować osobno; tokenów nie wolno przenosić między serwerami.
