# Bezpieczeństwo

## Zgłaszanie podatności

Nie zgłaszaj podatności w publicznych issue. Kanał prywatny: [GitHub Private Vulnerability Reporting](https://github.com/Farfive/mcpilot/security/advisories/new) (zakładka *Security → Report a vulnerability*). Odpowiedź i harmonogram poprawki ustala właściciel; projekt nie deklaruje SLA.

## Wspierane wersje

| Wersja | Wsparcie |
| --- | --- |
| 0.1.x (pilotaż) | poprawki bezpieczeństwa w gałęzi `main` |

## Gwarancje projektu

- Sekrety (tokeny OAuth, PAT, klucze) nie trafiają do modelu, wyjątków, logów ani zdarzeń audytu. Raporty pilotażu (`python -m mcpilot.pilot`) nie zawierają tokenów, adresów logowania, argumentów ani wyników.
- Wpisy rejestrów MCP nigdy nie są wykonywalne. Uruchamiane są wyłącznie manifesty zatwierdzone przez hosta (odcisk RFC 8785).
- Zapis lub wysyłka po niepewnym przekroczeniu czasu nie są powtarzane automatycznie.
- Brama zdalna wymaga tokenu hosta; link logowania u dostawcy jest jednorazowy i związany z przeglądarką właściciela.
- `python -m mcpilot setup` nie zapisuje sekretów w konfiguracji hosta MCP: klucz szyfrujący trafia do pęku kluczy systemu, PAT do zaszyfrowanego magazynu, a odczyt nigdy nie wymaga przekazania sekretu w argumentach polecenia.

## Ryzyka rezydualne (świadomie zaakceptowane)

- Instalator zapewnia izolację zależności, a nie sandbox systemu operacyjnego. Pakiety lokalne to zaufany kod zatwierdzony przez administratora.
- Polityka sprawdza literały IP; nazwa hosta z zatwierdzonego manifestu nie jest przypinana do adresu (zaufany administrator).
- `EncryptedFileSecretStore` jest przeznaczony dla jednego procesu i jednego klucza (brak rotacji). Wiele instancji wymaga własnego `SecretStore`.
- Oczekujące logowania i procesy stdio bramy zdalnej żyją w pamięci instancji.
- Tenant bramy zdalnej może uruchamiać serwery stdio na hoście bramy, jeśli manifest je zawiera.

Szczegóły i plan: [docs/architecture.md](docs/architecture.md#granice-bezpieczeństwa), [docs/gateway.md](docs/gateway.md).
