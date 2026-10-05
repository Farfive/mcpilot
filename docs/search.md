# Dobór MCP z kontekstu rozmowy

`mcpilot.search.CapabilityIndex` znajduje integracje pasujące do całego kontekstu (zadanie i ostatnie wiadomości) bez wywołania modelu i bez sieci. Model wywołuje jak dotąd `mcpilot_find_tools`, opcjonalnie z polem `context`. Nie dostaje katalogu, a jedynie gotowe narzędzia i krótkie sugestie.

## Algorytm

1. **Normalizacja (PL/EN).** Małe litery, usunięcie polskich znaków (`zgłoszenia` → `zgloszenia`), odrzucenie słów pomocniczych i poleceń („znajdź”, „porównaj”). Indeks przechowuje zarówno pełne słowa, jak i rdzenie (6 znaków): pełne słowo waży 1,0, sam rdzeń 0,4.
2. **Synonimy pojęć.** Np. *issue* ↔ ticket, zgłoszenie, Jira, GitHub; *docs* ↔ dokumentacja, wiki, Notion, Confluence. Dopisują terminy z wagą 0,5, więc nie wypierają słów użytkownika.
3. **Odmiana nazw produktów.** „w jirze” → `jira`, „gitlabie” → `gitlab`, „Asanie” → `asana`, „slacka” → `slack`. Dopasowanie obejmuje tylko nazwę z rejestru zakończoną polską końcówką przypadka. Ostatnia litera nazwy może się wymienić tylko wtedy, gdy to -a, -e lub -o. Dzięki temu „faktury”, „database” czy „bazy” nie stają się nazwami serwerów.
4. **BM25F.** Indeks odwrócony z wagami pól: nazwa i usługa ×3, narzędzia i capabilities ×2, opis ×1. W nazwach pomijane są człony domen (`com`, `io`…) i prefiks `io.github.<użytkownik>`, żeby słowo „github” nie dominowało połowy rejestru.
5. **Intencja dostawcy.** Jeśli użytkownik nazwał produkt (wprost lub przez mapę produkt → dostawca: Jira i Confluence → `atlassian`, Teams → `microsoft`), oficjalny serwer dostawcy (`com.atlassian/…`, nie `io.github.<ktoś>/…`) trafia na pierwsze miejsce. Za nazwę dostawcy uznawane jest tylko słowo rzadkie w korpusie, więc „database” czy „review” nimi nie są.
6. **Warstwy i sygnały.** Zatwierdzone integracje dostają premię, bo są wykonywalne. Wpisy `deprecated` dostają karę, a usunięte nie trafiają do indeksu.
7. **Pokrycie wielu wątków.** Wybór zachłanny: wynik, który powtarza już pokryte słowa, jest obniżany. Zapytanie o Jirę i Confluence dostaje osobne trafienia dla obu.

## Użycie w przepływie

- Router (aliasy usług) działa jak dotąd. Gdy nie rozpozna usługi albo zostają brakujące wymagania, indeks szuka w kontekście **wśród zatwierdzonych integracji**, a plan jest powtarzany z ich usługami.
- Jeśli nadal czegoś brakuje, wynik zawiera `suggestions`: maks. 3 wpisy z rejestru z `action: "needs_admin_approval"`. Mają tylko ID, opis i dopasowane słowa, bez endpointów, pakietów i poleceń. Nie da się ich wywołać.
- Brama lokalna buduje indeks przy starcie. Z `registry_sync: true` (domyślnie w `python -m mcpilot setup`) odświeża rejestr w tle (pełna synchronizacja raz, potem przyrostowa) i przebudowuje indeks. Brama zdalna buduje indeks per tenant.
- TypeScript nie ma jeszcze indeksu. Gdy samo zadanie nie wystarcza, dołącza `context` do planowania routera.

## Pomiar (rejestr z 2026-10-04, 39 321 wpisów + manifest pilotażu)

```sh
python -m mcpilot.search --eval spec/search_eval.json --cache <registry-cache.json> --manifest <approved.json>
python -m mcpilot.search "porównaj tickety w jirze z dokumentacją w confluence" --cache … --manifest …
```

| Zestaw | Przypadki | Trafienie na 1. miejscu | W pierwszej trójce | Uwagi |
| --- | --- | --- | --- | --- |
| `spec/search_eval.json` | 14 | 14/14 | 14/14 | zestaw strojenia, liczby zawyżone |
| `spec/search_eval_holdout.json`, pierwszy pomiar | 12 | 8/12 | 10/12 | **czysty wynik**: zapytania pisane po strojeniu |
| `spec/search_eval_holdout.json` po dwóch ogólnych poprawkach odmiany | 12 | 11/12 | 12/12 | już nie jest czystym holdoutem |

| Czas | Wartość |
| --- | --- |
| Budowa indeksu | ok. 0,7 s |
| Zapytanie (mediana) | ok. 10 ms |
| Zapytanie (maks.) | ok. 120 ms (długie zapytanie wielowątkowe) |

**Ograniczenia:**
- Zestawy są małe i napisane przez nas.
- Sugestie z rejestru to wpisy *odkryte*, nie *wspierane*. Rejestr nie mówi nic o jakości serwera.
- Wiarygodna metryka „trafności wyboru” z product.md wymaga zapytań od pilotażowych użytkowników z oceną, który wynik był właściwy.
