# Przebieg pilotażowy: <dostawca> (<RRRR-MM-DD>)

Generowany przez `python -m mcpilot.pilot --report …`. Ręcznie uzupełnia się tylko sekcję „Obserwacje”.

| Pole | Wartość |
| --- | --- |
| Status dowodu | Konto dostawcy / Demonstracyjne (lokalny fixture) |
| Werdykt | pass / pass_without_provider_revoke / fail |
| Integracja | `<id>` (odcisk manifestu) |
| Transport / endpoint | |
| Metoda autoryzacji | oauth / bearer; żądane scope |
| Konto (etykieta) | |
| Capability | |
| MCPilot | wersja |

| Krok protokołu | Wynik | Szczegóły (bez treści i sekretów) | Czas [ms] |
| --- | --- | --- | --- |
| logowanie i połączenie | | status, liczba logowań w przeglądarce | |
| mapowanie narzędzi (tools/list) | | zmapowane obecne / brakujące, liczba niezmapowanych | |
| odczyt | | narzędzie, klucze argumentów, liczba bloków, bajty | |
| ponowne użycie po restarcie | | status, nowe logowania (oczekiwane 0) | |
| odświeżenie tokenu | | pass / n/a (PAT lub brak refresh token) | |
| cofnięcie u dostawcy | | status po cofnięciu (oczekiwane ≠ ready) | |
| cofnięcie lokalne | | status po cofnięciu (oczekiwane auth_required) | |

Narzędzia serwera (`tools/list`): …

## Obserwacje

- Różnice względem migawki w `spec/providers/` (nazwy, wymagane parametry):
- Zachowanie logowania (ekran zgody, przekierowanie, błędy):
- Decyzja o statusie integracji:

Raport nie może zawierać tokenów, adresów logowania, wartości argumentów ani wyników narzędzi.
