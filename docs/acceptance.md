# Kryteria odbioru i status weryfikacji

Stan na **2026-10-04**. Każde kryterium ma status i dowód, który można uruchomić.

| Status | Znaczenie |
| --- | --- |
| **E2E lokalnie** | Prawdziwe procesy, protokół MCP i transport; bez zewnętrznych usług. |
| **E2E sieć** | Prawdziwa usługa publiczna (rejestr MCP, npm); test opcjonalny. |
| **Demonstracyjne** | Prawdziwe protokoły (MCP, HTTP, OAuth) i kod SDK; symulowany dostawca, zgoda użytkownika albo model. |
| **Wymaga konta** | Kod i konfiguracja gotowe; brak testu na koncie dostawcy lub kluczu API. |

```sh
python -m pytest                                        # 96 testów lokalnych + 1 opcjonalny sieciowy
MCPILOT_NETWORK_TESTS=1 python -m pytest tests/test_network.py
ruff check .
(cd ts && npm test)                                     # 17 testów TypeScript, w tym z serwerami Python
scripts/ci.sh                                           # lokalny odpowiednik CI + artefakty
```

## Ścieżka „zadanie → dobór MCP → połączenie → wykonanie”

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 1 | Import nie instaluje, nie łączy się, nie tworzy plików ani procesów | E2E lokalnie | `test_sdk::test_import_is_side_effect_free` |
| 2 | Zadanie z Notion i GitHub daje wymagania obu usług; Drive/Slack nie zastępują wskazanej usługi | E2E lokalnie | `test_sdk::test_flagship_task_requirements_keep_named_services`, `test_router_respects_service_account_policy_and_candidates` |
| 3 | Konto wskazane przez użytkownika jest zachowane; konto spoza polityki daje brak wyboru | E2E lokalnie | `test_router_respects_service_account_policy_and_candidates` |
| 4 | Istniejące połączenie podnosi ocenę; reranker LLM nie może wybrać adresu, pakietu ani integracji spoza kandydatów | E2E lokalnie (atrapa rerankera) | `test_existing_connection_breaks_ties_and_reranker_is_constrained` |
| 5 | Zmieniony manifest (np. endpoint) traci zatwierdzenie; HTTPS, adres publiczny, brak sekretów w URL | E2E lokalnie | `test_policy_pins_exact_manifest_and_endpoint_rules` |
| 6 | Lokalny serwer stdio: start na żądanie, oczyszczone środowisko, `tools/list`, `tools/call`, zamknięcie procesu | E2E lokalnie | `test_runtime::test_stdio_real_tools_environment_and_cross_task_cleanup`, `examples/local_task.py` |
| 7 | Połączenie Streamable HTTP z prawdziwym serwerem MCP | E2E lokalnie | `test_runtime::test_http_real_streamable_connection` |
| 8 | Ładowane są tylko zmapowane narzędzia dozwolonego skutku; budżet tokenów | E2E lokalnie | `test_stdio_task_to_call_hides_unmapped_and_disallowed_tools`, `test_token_budget_and_step_limit` |
| 9 | Walidacja argumentów i schematów; narzędzie niewydane w sesji jest odrzucane | E2E lokalnie | jw., `test_discovery_rejects_unknown_tools_and_malformed_arguments` |
| 10 | Oficjalny serwer filesystem: instalacja przypiętej wersji npm w izolowanym katalogu i odczyt | E2E sieć | `test_network::test_official_filesystem_server_installs_pinned_and_reads` |

## Logowanie i autoryzacja

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 11 | OAuth: PRM, metadane AS, DCR, PKCE S256, `resource`, minimalne scope | Demonstracyjne (fixture IdP) | `test_auth::test_oauth_pkce_discovery_minimal_scope_and_reuse`, `test_real_loopback_http_oauth_then_mcp_tool_call_and_reconnect` |
| 12 | Odrzucenie błędnego `state` i `iss`; treść błędów nie trafia do logów | Demonstracyjne | `test_oauth_rejects_state_and_issuer_mismatch`, `test_oauth_error_bodies_are_not_logged_or_exposed` |
| 13 | Rozszerzenie scope spoza polityki zablokowane przed przeglądarką; brak przyznanego scope ≠ gotowe | Demonstracyjne | `test_explicit_scope_escalation_is_blocked_before_browser`, `test_missing_granted_scope_cannot_be_reported_as_ready` |
| 14 | Przycisk logowania: URL tylko do UI, callback tylko dla znanego `state` tego samego użytkownika, odmowa/timeout/anulowanie → `auth_required` | E2E lokalnie | `test_login::*` |
| 15 | Logowanie trwające dłużej niż timeout MCP nie psuje połączenia; pętla agenta nie stoi (`auth_pending`) | Demonstracyjne | `test_unanswered_login_returns_auth_required_connection_not_error`, `test_gateway::test_login_does_not_block_the_agent_loop` |
| 16 | Ponowne użycie autoryzacji po restarcie bez logowania; odświeżenie tokenu po restarcie | Demonstracyjne | `test_oauth_e2e`, `test_refresh_after_restart_preserves_expiry_and_discovered_token_endpoint`, flagowy scenariusz |
| 17 | Wiele kont i izolacja użytkowników; cofnięcie dostępu | E2E lokalnie | `test_users_and_accounts_are_isolated`, `test_bearer_isolation_endpoint_user_account_and_revocation` |
| 18 | Szyfrowany magazyn bez jawnych tokenów, pliki 0600 | E2E lokalnie | `test_encrypted_store_persists_without_plaintext_and_uses_private_files` |
| 19 | `setup_required` z czytelnym powodem dla Drive/Slack | E2E lokalnie (logika) | `test_setup_required_is_explained_and_reviewed_candidates_report_it` |

## Wykonanie i workflow

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 20 | Flagowe zadanie: GitHub na wcześniej podłączonym koncie, Notion czeka na logowanie, wznowienie od niewykonanego kroku, szkic podsumowania niewysłany | Demonstracyjne | `test_login::test_flagship_notion_github_scenario`, `python -m examples.notion_github_task` |
| 21 | Zapis po niepewnym timeout nie jest powtarzany; przerwana mutacja wymaga rekoncyliacji | E2E lokalnie | `test_uncertain_write_is_never_replayed`, `test_stdio_timeout_never_replays_write`, `test_interrupted_mutation_needs_reconciliation_and_is_not_replayed` |
| 22 | Limity kroków i kosztu, także trwałe w workflow | E2E lokalnie | `test_token_budget_and_step_limit`, `test_concurrent_resume_is_rejected_and_step_limit_is_durable` |
| 23 | Trwały stan, izolacja przebiegów użytkowników, brak równoległego wykonania | E2E lokalnie | `test_resume_completes_persists_and_isolates_users`, `test_concurrent_resume_is_rejected_and_step_limit_is_durable` |
| 24 | Zdarzenia audytu bez sekretów i argumentów | E2E lokalnie | `test_audit_events_are_secret_free_and_sink_failures_do_not_break_calls` |

## Katalog, kontekst LLM, brama

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 25 | Wpisy rejestru niewykonywalne; cache offline; źródła prywatne rozdzielone; brak przekierowań | E2E lokalnie (atrapa HTTP) | `test_catalog::*` |
| 26 | Synchronizacja z oficjalnym MCP Registry: pełna, potem przyrostowa | E2E sieć (ręcznie, 2026-10-04) | 40 209 wpisów, 76 s, cache 14,4 MB; przyrost 1,0 s. Skrypt w sekcji „Pomiar rejestru” poniżej. |
| 27 | Model dostaje dwa meta-narzędzia; w żądaniach do modelu brak tokenów, URL logowania i endpointów | Demonstracyjne (atrapa Messages API) | `test_claude_agent::test_claude_agent_loop_uses_meta_tools_and_never_sees_secrets` |
| 28 | Prawdziwa pętla z Claude (`claude-opus-5-5`) | Wymaga klucza API | `python -m examples.claude_agent` |
| 29 | Brama MCP przez prawdziwe stdio z zagnieżdżonym serwerem lokalnym; brak poświadczeń → `connect_account` | E2E lokalnie | `test_gateway::test_gateway_cli_over_real_stdio_with_nested_local_server` |
| 30 | Adapter frameworka (LangChain) | E2E lokalnie | `python examples/langchain_tools.py` |

## Brama zdalna i logowanie przez elicytację URL

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 31 | Bez tokenu `401` z Protected Resource Metadata; JWT: podpis, `iss`, `aud`, `exp`, `sub` | E2E lokalnie | `test_remote::test_unauthenticated_requests_get_protected_resource_challenge`, `test_jwt_verifier_checks_signature_issuer_audience_and_expiry` |
| 32 | Konfiguracja JSON → brama chroniona JWT → wywołanie narzędzia; token z obcym `aud` odrzucony | E2E lokalnie | `test_remote_config_builds_jwt_protected_gateway_end_to_end` |
| 33 | Izolacja użytkowników (cudze ID narzędzia odrzucone) i tenantów; limit wywołań; zamykanie bezczynnych sesji | E2E lokalnie | `test_users_are_isolated_and_tenants_have_their_own_policy`, `test_rate_limit_and_idle_sessions_are_closed` |
| 34 | Elicytacja URL → strona bramy → dostawca → callback → wznowione wywołanie z narzędziami; URL nie trafia do modelu | Demonstracyjne (fixture IdP i przeglądarka) | `test_url_elicitation_login_completes_and_resumes_the_tool_call`, `python -m examples.remote_gateway_demo` |
| 35 | Przekazany link logowania nie podpina cudzego konta; callback bez ciasteczka odrzucony | E2E lokalnie | `test_forwarded_login_link_cannot_attach_another_browser` |
| 36 | Klient bez elicytacji URL: `finish_login` i statyczna `connect_page`; klient starszego protokołu: błąd `-32042` i ponowienie | E2E lokalnie | `test_clients_without_url_elicitation_…`, `test_legacy_clients_get_url_elicitation_required_error_then_retry` |
| 37 | Oficjalny klient TypeScript ↔ brama Python: negocjacja 2026-07-28, elicytacja URL, wywołanie | Demonstracyjne | `ts/test/oauth.test.ts` |

## TypeScript SDK

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 38 | Wspólne wektory: kanoniczny JSON (RFC 8785), odciski manifestów, 47 przypadków endpointów, wymagania, trasy z wynikami, klucze i ID, rejestr, discovery, workflow, Fernet | E2E lokalnie | `tests/test_vectors.py` (aktualność), `ts/test/vectors.test.ts` |
| 39 | stdio i Streamable HTTP z serwerami Python; ukrywanie niezmapowanych narzędzi, walidacja, izolowane środowisko, budżety, brak powtórzenia zapisu po przerwaniu (`AbortSignal`), audyt, workflow | E2E lokalnie | `ts/test/sdk.test.ts` |
| 40 | OAuth: przycisk, minimalne scope (+`offline_access`), odmowa obcego użytkownika, jednorazowy callback, ponowne użycie po restarcie, brak tokenów jawnie na dysku | Demonstracyjne (fixture IdP) | `ts/test/oauth.test.ts` |
| 41 | Przykład TS: zadanie → lokalny serwer stdio → wywołanie | E2E lokalnie | `cd ts && npm run example` |

## Uszczelnienia wykryte przy portowaniu

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 42 | `localhost.`, `*.localhost`, zapisy IPv4 `127.1` / `0x7f.0.0.1` / `2130706433`, końcowa liczbowa etykieta, `\`, białe znaki i brak authority są odrzucane w obu SDK | E2E lokalnie | wektory `endpoints` |

## Gotowość do pilotażu (cykl 2026-10-05)

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 43 | Budżet kroków i kosztu liczony per zadanie: długa sesja bramy działa dalej, 21. wywołanie w jednym zadaniu daje `BudgetExceeded` z prośbą o nowe zadanie | E2E lokalnie | `test_sdk::test_budget_is_per_task_not_per_session_so_gateway_sessions_keep_working`, `ts/test/sdk.test.ts` (budget is per task) |
| 44 | Zdarzenia audytu z `tenant`, `task_id`, `duration_ms`, ten sam kształt w Python i TS; `audit_log` w obu bramach; plik 0600 bez sekretów, argumentów i wyników | E2E lokalnie | `test_metrics::test_flagship_audit_log_yields_product_metrics`, `test_gateway_cli_over_real_stdio_with_nested_local_server`, `test_remote_config_builds_jwt_protected_gateway_end_to_end`, wektor `audit_event_fields` |
| 45 | Metryki pilotażu z product.md liczone z dziennika (czas do pierwszego odczytu, wznowienia po logowaniu, skuteczność połączeń, opóźnienie); trafność wyboru i koszt konta jawnie niemierzalne | Demonstracyjne (dziennik flagowego scenariusza) | `python -m mcpilot.metrics audit.jsonl`, `test_metrics.py` |
| 46 | Skrypt protokołu pilotażu: logowanie, mapowanie `tools/list`, odczyt, ponowne użycie po restarcie, wymuszone odświeżenie, cofnięcie u dostawcy (interaktywnie) i lokalne; raport bez sekretów | Demonstracyjne (fixture); na kontach: Wymaga konta | `test_pilot.py`, `python -m mcpilot.pilot`, [docs/pilots/](pilots/README.md) |
| 47 | Szablon GitHub zgodny z prawdziwym `github-mcp-server` v1.14.0 (nazwy narzędzi, wymagany `method` w `issue_read`) | E2E sieć (binarka serwera, `tools/list` bez ważnego tokenu; nie serwer hostowany) | `spec/providers/github-mcp-server-1.14.0.json`, `test_provider_snapshots.py` |
| 48 | Szablon Notion zgodny z dokumentacją narzędzi (`notion-search`, `notion-fetch`, tylko odczyt) | dokumentacja (bez konta); nazwy parametrów do potwierdzenia | `spec/providers/notion-mcp-docs-2026-10-05.json` |
| 49 | Prawdziwy host: Claude Code 2.1.284 → brama stdio i brama HTTP z tokenem hosta → odczyt | E2E lokalnie | `spec/hosts/*.jsonl`, [docs/hosts.md](hosts.md) |
| 50 | Cursor, VS Code (Copilot) | nie zweryfikowano (dokumentacja w [hosts.md](hosts.md)) | checklista ręczna w [hosts.md](hosts.md) |
| 51 | CI: ruff + pytest (Python 3.11–3.13), `npm test` (Node 20/22) z serwerami Python, artefakty z `SHA256SUMS`; spójna wersja Python/TS/CHANGELOG | E2E lokalnie (`scripts/ci.sh`, pytest na 3.11.13 i 3.13); workflow GitHub Actions zaimplementowany, ale nieuruchomiony (brak zdalnego repozytorium) | `.github/workflows/ci.yml`, `scripts/build_artifacts.sh`, `test_release.py` |

## Pakiet pilotażowy (Claude Code)

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 52 | `python -m mcpilot setup`: manifest, klucz w pęku kluczy systemu, zaszyfrowany PAT, rejestracja w Claude Code bez sekretów w konfiguracji hosta i bez nadpisania istniejącego wpisu | E2E lokalnie (prawdziwy macOS Keychain i Claude Code 2.1.284; GitHub jako lokalna atrapa) | `test_cli.py`, przebieg opisany w [hosts.md](hosts.md) |
| 53 | Restart hosta nie wymaga ponownego logowania: dwie sesje Claude Code korzystają z zapisanego dostępu | E2E lokalnie (atrapa dostawcy); na kontach: Wymaga konta | `test_pilot_kit_end_to_end_without_secrets_in_host_config`, audyt w [hosts.md](hosts.md) |
| 54 | `status` bez tokenów, `disconnect` usuwa dostęp (kolejne użycie → `connect_account`) i podaje instrukcję cofnięcia u dostawcy, `uninstall` usuwa rejestrację i klucz; brama bez klucza ostrzega, że tokeny są tylko w pamięci | E2E lokalnie | `test_cli.py` |

## Dobór MCP z kontekstu

| # | Kryterium | Status | Dowód |
| --- | --- | --- | --- |
| 55 | Wyszukiwanie w kontekście: odmiana nazw produktów, intencja dostawcy, pokrycie wielu wątków, kara dla `deprecated`, brak usuniętych; sugestie bez endpointów | E2E lokalnie | `test_search.py` |
| 56 | Router nie zna usługi → indeks znajduje zatwierdzoną integrację z `context`; brak zatwierdzonej → `suggestions` z `needs_admin_approval`, niewywoływalne | E2E lokalnie | `test_search::test_discovery_uses_context_search_for_unaliased_services_and_suggests_registry` |
| 57 | Trafność i szybkość na prawdziwym rejestrze (39 321 wpisów): zestaw odłożony — pierwszy pomiar 10/12 w pierwszej trójce, mediana zapytania ok. 10 ms | E2E sieć (ręcznie, 2026-10-05; cache rejestru nie jest w repozytorium) | [search.md](search.md), `spec/search_eval*.json` |

## Wymaga kont dostawców

| Integracja | Gotowe w kodzie | Brakujący dowód |
| --- | --- | --- |
| GitHub (`com.github/remote`, `/mcp/readonly`) | Szablon `github()`, tryb `bearer`, PAT przez magazyn lub `secrets` bramy | Odczyt issue na koncie z PAT; OAuth wymaga własnej GitHub App/OAuth App |
| Notion (`com.notion/mcp`) | Szablon `notion()`, OAuth z DCR i PKCE, przycisk logowania | Logowanie i wyszukiwanie w prawdziwym workspace |
| Google Drive | Kandydat `setup_required` | Dostęp do Developer Preview, projekt Cloud, klient OAuth, recenzja mapowania narzędzi |
| Slack | Kandydat `setup_required` | Zarejestrowana, dopuszczona aplikacja Slack; recenzja mapowania narzędzi |

Protokół testu na koncie: data, dostawca, transport, metoda autoryzacji, wykonany odczyt, ponowne użycie bez logowania, wynik cofnięcia dostępu. Bez tokenów i prywatnych treści w raporcie. Wykonuje go `python -m mcpilot.pilot`; definicja integracji wspieranej, lista potrzebnych kont i polecenia: [docs/pilots/README.md](pilots/README.md).

## Pomiar rejestru

```python
import asyncio, time
from pathlib import Path
from mcpilot.catalog import Catalog

async def main():
    path = Path(".mcpilot/registry-cache.json")
    start = time.time(); print(await Catalog(cache_path=path).sync(), time.time() - start)  # pełna
    start = time.time(); print(await Catalog(cache_path=path).sync(), time.time() - start)  # przyrostowa

asyncio.run(main())
```
