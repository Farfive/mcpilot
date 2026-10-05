"""Fast context → MCP integration search (BM25 over approved manifests and registry entries).

The whole conversation context (task plus recent messages) is normalized once
(Polish/English, diacritics removed, stop words dropped, prefix stemming,
synonym expansion) and scored against a prebuilt inverted index with field
weights. No model call, no network; a 40k-entry registry answers in milliseconds.

Two tiers, never mixed: approved integrations are executable and feed the router;
registry entries are suggestions for an administrator (``needs_admin_approval``)
without endpoints, packages or commands.

    python -m mcpilot.search "porównaj dokumentację z ticketami w jirze" --cache registry-cache.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .catalog import Catalog
from .models import Integration
from .router import SERVICE_ALIASES

_WORD = re.compile(r"[a-z0-9]+")
_STEM = 6  # prefix stemming: "dokumentacja"/"dokumentacji" → "dokume", "issues"/"issue" → "issue"
_STOP = set("""
a an and are as at be by can could do does for from get give how i in into is it its me my of on or our please
show some that the their them then there these this to us using want we what when where which who will with
would you your
a aby ale albo bo by czy dla do i ich jak jaki jakie jest juz ktore ktory mi mnie moje na nad nam nas nie o od
oraz po pod prosze przez sie sa ta tak te tego ten to tu w we z za ze zrob znajdz przygotuj porownaj sprawdz
pokaz daj chce chcialbym potrzebuje mozesz musze
""".split())
# Concept → equivalent words (PL/EN, product names). Expansion only adds query terms.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "issue": ("issue", "issues", "ticket", "bug", "zgloszenie", "zgloszenia", "jira", "github", "linear", "tracker"),
    "docs": ("dokumentacja", "documentation", "docs", "wiki", "notatki", "notes", "notion", "confluence", "page"),
    "file": ("plik", "pliki", "file", "files", "folder", "katalog", "dokument", "document", "drive", "dysk",
             "lokalny", "lokalnie", "local", "readme"),
    "chat": ("wiadomosc", "wiadomosci", "message", "messages", "chat", "kanal", "channel", "slack", "teams"),
    "mail": ("mail", "email", "poczta", "inbox", "gmail", "outlook"),
    "calendar": ("kalendarz", "calendar", "spotkanie", "meeting", "event", "wydarzenie"),
    "code": ("kod", "code", "repo", "repozytorium", "repository", "pull", "commit", "github", "gitlab"),
    "database": ("baza", "database", "sql", "postgres", "mysql", "tabela", "table", "query"),
    "crm": ("crm", "klient", "klienci", "customer", "salesforce", "hubspot", "lead"),
    "design": ("design", "figma", "makieta", "mockup"),
    "payment": ("platnosc", "platnosci", "payment", "faktura", "invoice", "stripe"),
}


def normalize(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower().replace("ł", "l"))
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return [w for w in _WORD.findall(folded) if len(w) > 1 and w not in _STOP]


_SYNONYM_STEMS = {concept: {w[:_STEM] for w in words} for concept, words in _SYNONYMS.items()}


# Product names that live under another vendor's namespace (com.atlassian/..., com.microsoft/...).
PRODUCT_VENDORS = {
    "jira": "atlassian", "confluence": "atlassian", "bitbucket": "atlassian", "trello": "atlassian",
    "outlook": "microsoft", "teams": "microsoft", "sharepoint": "microsoft", "onedrive": "microsoft",
    "azure": "microsoft", "excel": "microsoft", "gmail": "google", "gdrive": "google", "bigquery": "google",
}


def _keys(words: Iterable[str], word_weight: float, stem_weight: float) -> dict[str, float]:
    keys: dict[str, float] = {}
    for word in words:
        keys[f"w:{word}"] = max(keys.get(f"w:{word}", 0), word_weight)
        keys[f"s:{word[:_STEM]}"] = max(keys.get(f"s:{word[:_STEM]}", 0), stem_weight)
    return keys


def query_terms(context: str) -> dict[str, float]:
    """Index keys with weights: exact words 1.0, shared stems 0.4; synonyms of matched concepts 0.5/0.2."""
    words = normalize(context)
    terms = _keys(words, 1.0, 0.4)
    stems = {w[:_STEM] for w in words}
    for concept, concept_stems in _SYNONYM_STEMS.items():
        if concept_stems & stems:
            for key, weight in _keys(_SYNONYMS[concept], 0.5, 0.2).items():
                terms.setdefault(key, weight)
    return terms


# Polish case endings (diacritics folded): jira → jirze/jiry/jirę, gitlab → gitlabie/gitlaba/gitlabem.
_ENDINGS = ("a", "e", "i", "y", "u", "o", "ie", "ze", "rze", "ce", "dze", "cie", "zie", "em", "om", "owi",
            "ach", "ami", "ow")


def _inflected(word: str, names: Iterable[str]) -> str | None:
    """Map an inflected product name to the name ('jirze' → 'jira', 'gitlabie' → 'gitlab').

    Only a known name followed by a Polish case ending matches (optionally replacing the
    name's last letter), so 'faktury', 'database' or 'lokalny' never turn into vendor names."""
    names = names if isinstance(names, (set, frozenset)) else set(names)
    if word in names:
        return None
    for cut in (0, 1):
        for ending in _ENDINGS:
            if word.endswith(ending):
                stem = word[: len(word) - len(ending)]
                for name in ((stem,) if cut == 0 else ()):
                    if name in names and len(name) >= 4:
                        return name
                if cut == 1 and len(stem) >= 3:
                    # Only a final -a/-e/-o alternates in declension (asana → asanie, stripe → stripie).
                    matches = [n for n in names if len(n) >= 4 and n[:-1] == stem and n[-1] in "aeo"]
                    if len(matches) == 1:
                        return matches[0]
    return None


@dataclass(frozen=True)
class Hit:
    id: str
    tier: Literal["approved", "registry"]
    score: float
    description: str
    matched: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        """Model-safe view: no endpoints, packages or commands exist in the index at all."""
        return {"id": self.id, "description": self.description[:200], "score": round(self.score, 2),
                "matched": [m.split(":", 1)[1] for m in self.matched if m.startswith("w:")]}


@dataclass
class _Doc:
    id: str
    tier: str
    vendor: str
    official: bool
    description: str
    fields: dict[str, list[str]]
    penalty: float


_WEIGHTS = {"name": 3.0, "service": 3.0, "tools": 2.0, "capabilities": 2.0, "description": 1.0}


class CapabilityIndex:
    """BM25F-style inverted index. Build once per catalog snapshot; queries are read-only."""

    def __init__(self, documents: Iterable[_Doc], *, k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = list(documents)
        # Vendor labels can be boosted; any word of any server name can absorb Polish inflection.
        self.names = frozenset({d.vendor for d in self.docs if d.official and len(d.vendor) >= 4} | set(PRODUCT_VENDORS))
        self.name_words = frozenset({w for d in self.docs for w in d.fields["name"] if len(w) >= 4}) | self.names
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        lengths = []
        for index, doc in enumerate(self.docs):
            weighted: Counter[str] = Counter()
            for field, words in doc.fields.items():
                for word in words:
                    weighted[f"w:{word}"] += _WEIGHTS[field]
                    weighted[f"s:{word[:_STEM]}"] += _WEIGHTS[field]
            lengths.append(sum(weighted.values()))
            for key, frequency in weighted.items():
                self.postings[key].append((index, frequency))
        self.lengths = lengths
        self.average = sum(lengths) / len(lengths) if lengths else 1.0

    @classmethod
    def from_catalog(cls, catalog: Catalog, *, registry: bool = True) -> CapabilityIndex:
        docs = [_approved_doc(i) for i in catalog.all()]
        approved = {d.id for d in docs}
        if registry:
            docs += [_registry_doc(entry) for entry in catalog.registry_servers() if entry["id"] not in approved]
        return cls(docs)

    def named_vendors(self, context: str) -> set[str]:
        """Vendors the user named: product names, or rare words equal to an official vendor label.
        Common words ('database', 'review') are not vendors even if some namespace uses them."""
        limit = max(50, len(self.docs) // 50)
        named = set()
        for word in normalize(context):
            word = _inflected(word, self.names) or word
            if word in PRODUCT_VENDORS:
                named.add(PRODUCT_VENDORS[word])
            elif word in self.names and len(self.postings.get(f"w:{word}", ())) <= limit:
                named.add(word)
        return named

    def search(self, context: str, *, tier: str | None = None, limit: int = 5, min_score: float = 0.5) -> list[Hit]:
        terms = query_terms(context)
        for word in normalize(context):  # 'w jirze' also searches for 'jira', 'slacka' for 'slack'
            name = _inflected(word, self.name_words)
            if name:
                terms.update(_keys([name], 1.0, 0.4))
        if not terms:
            return []
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, set[str]] = defaultdict(set)
        total = len(self.docs)
        for key, weight in terms.items():
            postings = self.postings.get(key, ())
            if not postings:
                continue
            idf = math.log(1 + (total - len(postings) + 0.5) / (len(postings) + 0.5))
            for index, frequency in postings:
                if tier and self.docs[index].tier != tier:
                    continue
                norm = frequency + self.k1 * (1 - self.b + self.b * self.lengths[index] / self.average)
                scores[index] += weight * idf * frequency * (self.k1 + 1) / norm
                matched[index].add(key)
        vendors = self.named_vendors(context)
        candidates = []
        for index, score in scores.items():
            doc = self.docs[index]
            if doc.official and doc.vendor in vendors:
                score += 1000.0  # the user named this product: its vendor's own server ranks first
            if doc.tier == "approved":
                score += 5.0  # executable now, reviewed by the administrator
            score -= doc.penalty
            if score >= min_score:
                candidates.append(Hit(doc.id, doc.tier, score, doc.description,  # type: ignore[arg-type]
                                      tuple(sorted(matched[index]))))
        candidates.sort(key=lambda h: (-h.score, h.id))
        return _diversify(candidates[:50], limit)


def _diversify(candidates: list[Hit], limit: int) -> list[Hit]:
    """Greedy coverage: a hit that only repeats already covered words is demoted, so each
    intent of a multi-intent context (e.g. Jira and Confluence) gets its own result."""
    chosen: list[Hit] = []
    covered: set[str] = set()
    pool = list(candidates)
    while pool and len(chosen) < limit:
        def adjusted(hit: Hit) -> float:
            words = {m for m in hit.matched if m.startswith("w:")}
            return hit.score if not words or words - covered else hit.score * 0.6
        best = max(pool, key=lambda h: (adjusted(h), -len(h.id)))
        pool.remove(best)
        chosen.append(best)
        covered |= {m for m in best.matched if m.startswith("w:")}
    return chosen


def _vendor(identifier: str) -> str:
    """'com.notion/mcp' → 'notion'; 'io.github.user/x' → 'user'; 'ai.smithery/notion' → 'smithery'."""
    namespace = identifier.partition("/")[0].split(".")
    return namespace[-1] if len(namespace) > 1 else namespace[0]


def _official(identifier: str) -> bool:
    """Reverse-DNS vendor namespace (com.stripe/…); personal io.github.<user> only when user == vendor."""
    labels = identifier.partition("/")[0].split(".")
    if len(labels) < 2:
        return False
    return not (labels[:2] == ["io", "github"] and len(labels) == 3 and labels[2] != "github")


_HOSTING = {"com", "io", "ai", "app", "dev", "net", "org", "co", "sh", "tech", "eu", "de", "pl", "uk", "me"}


def _name_words(identifier: str) -> list[str]:
    """Meaningful name words: drop TLD labels and the io.github hosting prefix of personal namespaces."""
    namespace, _, rest = identifier.partition("/")
    labels = namespace.split(".")
    if labels[:2] == ["io", "github"] and len(labels) > 2:
        labels = labels[2:]
    labels = [label for label in labels if label not in _HOSTING]
    return normalize(" ".join(labels) + " " + re.sub(r"[-_.]", " ", rest))


def _approved_doc(integration: Integration) -> _Doc:
    return _Doc(
        id=integration.id, tier="approved", official=True,
        vendor=_vendor(integration.id) if "." in integration.id else integration.service,
        description=f"{integration.publisher}: {', '.join(integration.capabilities)}",
        fields={"name": _name_words(integration.id),
                "service": normalize(" ".join([integration.service.replace("-", " "), integration.publisher,
                                               *SERVICE_ALIASES.get(integration.service, ())])),
                "tools": normalize(" ".join(n.replace("_", " ").replace("-", " ") for n in integration.tools)),
                "capabilities": normalize(" ".join(c.replace(".", " ") for c in integration.capabilities)),
                "description": []},
        penalty=0.0)


def _registry_doc(entry: dict[str, Any]) -> _Doc:
    name = entry["id"]
    status = entry.get("registry_status")
    return _Doc(
        id=name, tier="registry", vendor=_vendor(name), official=_official(name),
        description=entry.get("description", ""),
        fields={"name": _name_words(name),
                "service": [], "tools": [], "capabilities": [],
                "description": normalize(entry.get("description", ""))},
        penalty=2.0 if status == "deprecated" else 0.0)


def _expected(hit_ids: list[str], expected: str) -> bool:
    return any(expected[1:] in i for i in hit_ids) if expected.startswith("~") else expected in hit_ids


def evaluate(index: CapabilityIndex, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """hit@1 / hit@3 for expect_any, all-in-top-5 for expect_all, query latency percentiles."""
    rows, latencies = [], []
    for case in cases:
        started = time.perf_counter()
        ids = [h.id for h in index.search(case["query"], limit=5)]
        latencies.append((time.perf_counter() - started) * 1000)
        if "expect_all" in case:
            ok1 = ok3 = all(_expected(ids, e) for e in case["expect_all"])
        else:
            ok1 = any(_expected(ids[:1], e) for e in case["expect_any"])
            ok3 = any(_expected(ids[:3], e) for e in case["expect_any"])
        rows.append({"query": case["query"], "hit@1": ok1, "hit@3": ok3, "top": ids[:3]})
    ordered = sorted(latencies)
    return {"cases": len(rows), "hit@1": round(sum(r["hit@1"] for r in rows) / len(rows), 3),
            "hit@3": round(sum(r["hit@3"] for r in rows) / len(rows), 3),
            "query_ms_p50": round(ordered[len(ordered) // 2], 2), "query_ms_max": round(ordered[-1], 2),
            "misses": [r for r in rows if not r["hit@3"]], "rows": rows}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mcpilot.search")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--eval", help="labelled cases JSON (spec/search_eval.json)")
    parser.add_argument("--cache", help="registry cache JSON written by Catalog.sync")
    parser.add_argument("--manifest", help="approved manifest (list or {'integrations': [...]})")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    catalog = Catalog(cache_path=Path(args.cache) if args.cache else None)
    if args.manifest:
        catalog.load_manifest(args.manifest)
    started = time.perf_counter()
    index = CapabilityIndex.from_catalog(catalog)
    built = time.perf_counter()
    if args.eval:
        report = evaluate(index, json.loads(Path(args.eval).read_text())["cases"])
        report.pop("rows")
        print(json.dumps({"index_docs": len(index.docs), "build_ms": round((built - started) * 1000, 1), **report},
                         indent=2, ensure_ascii=False))
        return
    if not args.query:
        parser.error("query or --eval is required")
    hits = index.search(args.query, limit=args.limit)
    done = time.perf_counter()
    print(json.dumps({"index_docs": len(index.docs), "build_ms": round((built - started) * 1000, 1),
                      "query_ms": round((done - built) * 1000, 2),
                      "hits": [{**h.public(), "tier": h.tier} for h in hits]}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
