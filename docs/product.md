# Produkt i kolejne etapy

MCPilot skraca drogę od celu użytkownika do użytecznego narzędzia. Pierwszy przypadek użycia to znalezienie dokumentacji w Notion, odczyt issue GitHub i przekazanie wyników hostowi do podsumowania. Host dostarcza model, pętlę agenta i interfejs logowania; SDK wybiera dopuszczone integracje, zarządza sesjami oraz egzekwuje uprawnienia. Import nie uruchamia procesów ani instalacji.

## Zakres MVP i kryteria odbioru

MVP powinno pokazać spójną ścieżkę `task → capabilities → plan → connect → tools → call`. Rejestrowane wyniki należy oznaczać jako lokalne end-to-end, demonstracyjne albo wymagające konta dostawcy. Potwierdzona dokumentacja integracji nie zastępuje testu konta.

| Kryterium | Dowód odbioru |
| --- | --- |
| Dwa transporty | Rzeczywista sesja HTTP z lokalnym serwerem testowym oraz rzeczywisty proces stdio; `initialize`, `tools/list`, `tools/call`, zamknięcie |
| Logowanie i wznowienie | Authorization Code z PKCE, odmowa błędnego state, token przechowywany poza modelem, wznowienie niedokończonego kroku |
| Ponowne użycie dostępu | Kolejne zadanie korzysta z istniejącego tokenu/połączenia bez ponownego logowania; test odświeżania i cofnięcia |
| Dobór z kontekstu | Jawne wskazanie Notion i GitHub zachowane; router odrzuca obcy endpoint lub integrację proponowaną przez model |
| Granice uprawnień | Odmowa narzędzia spoza polityki, walidacja JSON Schema, rozdzielenie kont i użytkowników |
| Ograniczony kontekst | Host otrzymuje wybrane narzędzia mieszczące się w budżecie, a nie pełny katalog |
| Przewidywalne wykonanie | Limit kroków i kosztów, bezpieczne retry odczytów; brak automatycznego powtórzenia zapisu po niepewnym timeout |
| Prawdziwe SaaS | Oddzielny test na uprawnionym koncie Notion/GitHub; bez niego integracje pozostają oznaczone jako wymagające konta |

Aktualny status każdego kryterium i polecenia weryfikacji: [acceptance.md](acceptance.md).

Pierwsza wersja nie obiecuje instalacji dowolnego serwera z rejestru, automatycznego uzyskania zgody administratora ani dostępu do wszystkich usług. Domyślnym scenariuszem jest odczyt. Przygotowanie podsumowania nie wysyła wiadomości; wysyłanie otrzymuje oddzielną capability i kontrolę uprawnień.

## Open source i płatna usługa

Proponowany model to SDK na licencji MIT oraz opcjonalna usługa zespołowa. Wiążący zakres licencji określa plik `LICENSE` w wydaniu. Rdzeń ma działać bez konta MCPilot i bez centralnego pośrednika dla treści.

| Open source SDK | Płatne funkcje zespołowe |
| --- | --- |
| Asynchroniczne API Python, transporty i adaptery auth | Utrzymywanie aplikacji OAuth, rotacja i magazyn sekretów z KMS |
| Router deterministyczny i interfejs rerankera | Organizacje, SSO, przypisywanie ról i administracja połączeniami |
| Lokalny cache, synchronizacja katalogu, prywatne źródła wskazane przez hosta | Hostowane katalogi prywatne, recenzja, podpisywanie i dystrybucja manifestów |
| Lokalne reguły polityki i zdarzenia audytu | Centralne polityki, retencja, wyszukiwanie i eksport audytu |
| Lokalny stan workflow i adapter magazynu sekretów | Usługa wznawiania workflow, limity organizacji, monitoring i SLA |

Podstawowe bezpieczeństwo, możliwość odmowy narzędzia i eksport własnych danych pozostają w SDK. Płatna wartość to utrzymanie oraz kontrola wielu użytkowników i środowisk. Usługa nie obchodzi zasad dostawców: np. własna aplikacja Slack nadal wymaga dopuszczonego sposobu dystrybucji.

Proponowane rozliczenie: abonament organizacji z limitem aktywnych połączeń, dalej opłata za aktywne połączenia lub zarządzane wywołania. Bez marży od tokenów modeli w pierwszej wersji. Cenę należy ustalić po pilotażu; dokument nie podaje fikcyjnego cennika. Mierzymy czas do pierwszego skutecznego odczytu, odsetek zadań wznowionych po logowaniu, trafność wyboru, skuteczność połączeń, opóźnienie oraz koszt utrzymania aktywnego konta.

## Brama MCP

Brama jest opcjonalną warstwą nad tym samym silnikiem. Wariant lokalny (stdio, jeden użytkownik) i zdalny (Streamable HTTP, wielu użytkowników, tenanty) są częścią MVP; konfiguracja hostów: [gateway.md](gateway.md). Wystawia dwa narzędzia discovery i kontrolowanego wywołania, a użytkownika i politykę bierze z konfiguracji bramy (wariant lokalny) albo z uwierzytelnionej sesji hosta (wariant zdalny). Nie przyjmuje dowolnego URL ani polecenia shell z argumentów modelu. Wynik downstream nie zmienia konfiguracji bramy.

Host musi raz dodać adres bramy Streamable HTTP albo polecenie lokalnego procesu stdio oraz uzyskać do niej dostęp. Biblioteka nie może dodać samej siebie do niepowiązanego hosta przez sam import. Autoryzacja host → brama jest osobna od brama → serwer dostawcy. Kontrolki logowania mogą wymagać obsługi linków lub powrotu do UI aplikacji; utrata połączenia MCP nie może usuwać trwałego stanu workflow.

Wariant zdalny ma izolację użytkowników i tenantów, osobny `redirect_uri` bramy dla OAuth dostawców, zaszyfrowane sekrety downstream kluczowane per użytkownik, limity ruchu i logowanie przez elicytację URL. Przed wydaniem produkcyjnym potrzebne są: test z IdP organizacji, `SecretStore` na KMS/Vault, wspólny stan wielu instancji i proces obsługi incydentów. Lokalnej bramy stdio nie należy wystawiać jako usługi wieloużytkownikowej.

## Plan wydań

1. **MVP Python (zrealizowane):** stabilny kontrakt API, transporty HTTP/stdio, OAuth z przyciskiem logowania, workflow ze wznowieniem, warstwa discovery, lokalna brama stdio, synchronizacja Registry. Osobny pilotaż Notion/GitHub na rzeczywistych kontach.
2. **Zdalna brama MCP (zrealizowane jako MVP):** Streamable HTTP dla wielu użytkowników, autoryzacja MCP host → brama (JWT/JWKS), polityka per tenant, sesje i procesy per użytkownik, limity, logowanie przez elicytację URL ze stroną bramy wiążącą przeglądarkę z użytkownikiem. Szczegóły: [gateway.md](gateway.md).
3. **TypeScript SDK (zrealizowane jako MVP):** ten sam kontrakt danych co Python, potwierdzony wspólnymi wektorami; `Promise`, `AbortSignal`, nieblokujące logowanie, stdio tylko w Node, magazyn poświadczeń zgodny z Pythonem. Szczegóły: [typescript.md](typescript.md).
4. **Zarządzane połączenia:** certyfikacja adapterów na kontach testowych, `SecretStore` na KMS/Vault, wspólny stan oczekujących logowań dla wielu instancji bramy, obserwowalność, pilotaże Drive/Slack po spełnieniu wymagań dostawców.
5. **Dalej:** testy z IdP organizacji i przeglądarkowymi hostami MCP, wystawianie wybranych narzędzi downstream jako narzędzi pierwszego poziomu, hostowany katalog prywatny z podpisanymi manifestami.

Kolejność zależy od dowodów z pilotażu. Decyzję o oznaczeniu integracji jako wspieranej podejmujemy po testach kompatybilności i autoryzacji, nie po samym odnalezieniu manifestu.
