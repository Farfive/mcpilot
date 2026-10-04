# Katalog

`Catalog` utrzymuje dwa oddzielne zbiory: wykonywalne manifesty dodane przez zaufany kod hosta oraz informacyjne wpisy pobrane z rejestrów. Sieć i cache nigdy nie tworzą uprawnień do uruchomienia serwera.

```python
from pathlib import Path

from mcpilot.catalog import Catalog

catalog = Catalog(cache_path=Path(".mcpilot/registry-cache.json"))

# Plik dostarcza administrator aplikacji; nie jest wynikiem modelu.
catalog.load_manifest(Path("approved-integrations.json"))
approved_definitions = catalog.all()

# Jawna operacja sieciowa; aktualizacja cache nie zmienia approved_definitions.
sync_status = await catalog.sync()
untrusted_summaries = catalog.discovered(limit=20)

# Opcjonalna praca w tle, opt-in przez hosta.
catalog.start_sync(interval=3600)
try:
    ...  # cykl życia aplikacji
finally:
    await catalog.close()
```

Przykład z `await` należy umieścić w funkcji asynchronicznej hosta. `approved_definitions` oznacza tu lokalne definicje administratora; dopuszczenie ich wykonania nadal należy do polityki runtime.

| API | Zachowanie |
| --- | --- |
| `Catalog(integrations=(), cache_path=None)` | Kopiuje manifesty hosta; opcjonalnie czyta lokalny cache. Nie łączy się z siecią. |
| `add(integration)` | Dodaje lub zastępuje jeden manifest z zaufanego kodu hosta. |
| `get(id)` / `all()` | Zwraca głębokie kopie tylko manifestów hosta; nieznany ID daje `KeyError`. |
| `load_manifest(path)` | Waliduje cały plik JSON przed zmianą katalogu; lista `Integration` lub obiekt `{"integrations": [...]}`. Zwraca listę. |
| `await sync(sources=..., client=None, max_pages=1000, page_size=100, full_refresh_after=86400)` | Pierwsza synchronizacja i każda po `full_refresh_after` sekundach od ostatniej pełnej pobiera kompletny snapshot; pozostałe pobierają zmiany (`updated_since`). Zwraca `sync_status()`. |
| `discovered(limit=20)` | Zwraca tuple opisów z `untrusted=True`, bez endpointów, poleceń i pakietów. Pomija usunięte wpisy. |
| `sync_status()` | Stan per źródło: `last_sync`, `last_full_sync`, `last_attempt`, kod `error`, liczba rekordów wraz z tombstones. |
| `start_sync(interval=3600, ...)` | Uruchamia jeden task z natychmiastową synchronizacją i kolejnymi próbami co interwał. |
| `await close()` | Anuluje i odbiera task synchronizacji. Nie zamyka klienta HTTP należącego do hosta. |

Domyślne źródło to `https://registry.modelcontextprotocol.io/v0.1/servers`. Można przekazać adres bazowy rejestru albo pełną ścieżkę `/v0.1/servers`. Wymagane jest HTTPS, poza localhost do testów. URL nie może zawierać poświadczeń, query ani fragmentu. Prywatny rejestr używa tego samego kontraktu i może otrzymać klienta `httpx2.AsyncClient` z autoryzacją hosta; klient z sekretem powinien obsługiwać wyłącznie odpowiadający mu rejestr.

Synchronizacja używa `version=latest`, `include_deleted=true` i nieprzezroczystego `metadata.nextCursor`. Synchronizacja przyrostowa dodaje `updated_since` (RFC 3339) równe czasowi rozpoczęcia poprzedniej synchronizacji minus 5 minut zakładki i scala wpisy po nazwie; usunięcia przychodzą jako wpisy ze statusem `deleted`. Pełne odświeżenie co `full_refresh_after` rozlicza wszystko, czego przyrost nie widzi (np. wpisy, które zniknęły bez zmiany statusu). Ogranicza liczbę stron, rozmiar odpowiedzi i opisów. Nie podąża za przekierowaniami. Powtarzający się cursor, niepoprawny rekord albo błąd którejkolwiek strony zachowuje poprzedni snapshot tego źródła. Inne źródła mogą odświeżyć się niezależnie. Usunięcia i nieobecność wcześniej znanego wpisu są rozliczane dopiero po pełnym pobraniu.

Snapshot jest zapisywany do pliku tymczasowego z trybem 0600 i atomowo zastępuje cache. Cache nie zawiera sekretów uwierzytelniania; opisy nadal stanowią niezaufane dane. Uszkodzony cache daje `cache_error="invalid_cache"` i nie wpływa na manifesty hosta. `last_sync` pozwala hostowi pokazać wiek danych offline. Błędy raportowane są kodem klasy wyjątku, bez treści odpowiedzi i nagłówków.

Pomiar 2026-10-04 na oficjalnym rejestrze: 40 209 wpisów (z tombstones), pełna synchronizacja 76 s, cache 14,4 MB, synchronizacja przyrostowa 1,0 s. Domyślny limit 1000 stron (100 000 wpisów) i 128 MB cache zostawia zapas na wzrost; przekroczenie limitu stron nie zatwierdza snapshotu i zgłasza `ValueError` w `sync_status()`. Sygnatury manifestów i hostowana usługa katalogu są kolejnymi etapami. Namespace wydawcy z rejestru nie zastępuje audytu adaptera, polityki organizacji ani potwierdzenia faktycznych narzędzi przez MCP. Źródła protokołu: [Registry API](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/generic-registry-api.md), [Registry OpenAPI](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/openapi.yaml), sprawdzone 2026-10-04.
