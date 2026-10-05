# Zatwierdzanie serwerów z MCP Registry

Model może zaproponować dowolny z ok. 38 tys. aktywnych serwerów w rejestrze, ale **uruchomić może tylko serwer zatwierdzony przez człowieka**. Zatwierdzenie to jedno polecenie w terminalu:

```sh
python -m mcpilot approve com.atlassian/atlassian-mcp-server
```

## Przepływ

1. W rozmowie model wywołuje `mcpilot_find_tools`. Jeśli nic zatwierdzonego nie pasuje, wynik zawiera `suggestions`. Brama lokalna dołącza do każdej pozycji pole `user_runs`, np. `python -m mcpilot approve com.atlassian/atlassian-mcp-server --config …`. Model przekazuje je użytkownikowi.
2. Użytkownik uruchamia polecenie. MCPilot pobiera pełny wpis z rejestru (`GET /v0.1/servers/{name}/versions/latest`), ponieważ lista rejestru nie zawiera adresów ani pakietów. Następnie pokazuje:
   - nazwę, wersję i status w rejestrze;
   - wydawcę i informację, czy to oficjalna przestrzeń nazw dostawcy (`com.atlassian/…`), czy konto prywatne (`io.github.<ktoś>/…`);
   - adres lub pakiet oraz sposób logowania.
3. Po potwierdzeniu („Połączyć się, aby pobrać listę narzędzi?”) MCPilot łączy się z serwerem. Jeśli serwer żąda logowania, otwiera się przeglądarka (OAuth MCP: PKCE, DCR, minimalne scope). Serwer publiczny nie wymaga logowania i zostaje zapisany jako `auth: none`.
4. MCPilot pokazuje narzędzia serwera:
   - `[odczyt]` — narzędzia z adnotacją `readOnlyHint: true`;
   - `[pominięte, zapis]` — wszystkie pozostałe.
5. Drugie potwierdzenie zapisuje manifest w `approved-integrations.json`, dodaje capability `<usługa>.read` w `gateway.json` i dopisuje rekord do `approvals.jsonl` (czas, wersja, odcisk, narzędzia).
6. Uruchomiona brama wykrywa zmianę plików przy następnym `mcpilot_find_tools` i przeładowuje zatwierdzenia oraz indeks wyszukiwania. Hosta nie trzeba restartować. Zadanie w rozmowie toczy się dalej.

Wycofanie usuwa manifest i lokalne poświadczenia. Wydane już ID narzędzi zwracają `PolicyDenied`:

```sh
python -m mcpilot remove com.atlassian/atlassian-mcp-server
```

## Zasady bezpieczeństwa

| Zasada | Jak jest egzekwowana |
| --- | --- |
| Zatwierdza człowiek | `approve` wymaga terminala (stdin i stdout to TTY). Polecenie uruchomione przez model w tle kończy się błędem. Brama nie ma narzędzia do zatwierdzania. |
| Każde połączenie osobno | Nie ma automatycznego zatwierdzania, także dla oficjalnych dostawców. |
| Domyślnie tylko odczyt | Mapowane są tylko narzędzia z `readOnlyHint: true`. Adnotacja pochodzi od serwera, więc decyduje osoba, która widzi listę. |
| Zapis osobno | `--include-write` mapuje pozostałe narzędzia jako `write` (capability `<usługa>.write`) dopiero po wpisaniu słowa `zapis`. |
| Przypięta wersja | Manifest zapisuje wersję z rejestru, adres lub pakiet i mapę narzędzi. Odcisk SHA-256 obejmuje całość, więc każda zmiana wymaga nowego zatwierdzenia. |
| Kod lokalny | Pakiety wymagają `--local` (npm na tym komputerze) albo `--container` (Docker). Przed uruchomieniem pojawia się ostrzeżenie „To uruchomi kod na Twoim komputerze” i trzeba wpisać `uruchom`. |
| Sekrety | Token lub klucz API wpisuje się w ukrytym polu (`getpass`). Trafia do zaszyfrowanego magazynu, nigdy do manifestu, konfiguracji hosta ani modelu. |

## Co da się zatwierdzić automatycznie

| Wpis w rejestrze | Wynik |
| --- | --- |
| Zdalny `streamable-http` bez wymaganych nagłówków | OAuth (logowanie tylko na żądanie serwera) albo `none` |
| Zdalny z wymaganym sekretnym nagłówkiem | `bearer` (dla `Authorization`) lub `api_key` z nazwą nagłówka; `--auth token` wymusza token, gdy nagłówek jest opcjonalny |
| Pakiet npm + `--local` | `PackageSpec` z dokładną wersją; nazwa pliku wykonywalnego pochodzi z metadanych npm |
| Pakiet npm / PyPI / obraz OCI + `--container` | `docker run -i --rm --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 1g`. npm działa w `node:22-alpine`, PyPI w obrazie `uv`. Obraz OCI musi mieć tag lub skrót. |
| Odmowa | Adres z szablonem (`{tenant}`), tylko SSE, wymagane niesekretne nagłówki, zmienne lub argumenty bez wartości, więcej niż jeden wymagany sekret, PyPI bez kontenera, wpis `deleted` |

Odmowa nie blokuje integracji na stałe: taki manifest można napisać ręcznie według [integrations.md](integrations.md).

## Status dowodów

| Element | Status |
| --- | --- |
| Rejestr → zatwierdzenie → przeładowanie bramy → wywołanie → wycofanie (serwer z adnotacjami na loopback) | E2E lokalnie (`test_approval.py`) |
| Serwer z OAuth: logowanie podczas zatwierdzenia, brama używa tego samego dostępu bez ponownego logowania | Demonstracyjne (fixture dostawcy tożsamości, prawdziwy protokół) |
| `com.microsoft/microsoft-learn-mcp` z prawdziwego rejestru: 3 narzędzia do odczytu, `auth: none`; brama znajduje je w rozmowie i wywołuje | E2E sieć (ręcznie, 2026-10-05) |
| Atlassian, Stripe i inne serwery z logowaniem | Wymaga konta |
| Tryb `--container` | Tylko budowa polecenia; brak przebiegu z działającym Dockerem |
| Zatwierdzanie w oknie hosta (elicytacja) zamiast terminala | Nie zbudowane |

**Znane ograniczenie:** limit `tool_tokens` (domyślnie 4000) może uciąć część narzędzi serwera z długimi opisami. Wynik ma wtedy `truncated: true`. Przy Microsoft Learn zwrócone zostały 2 z 3 narzędzi.
