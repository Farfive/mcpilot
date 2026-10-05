"""Context → MCP search: Polish inflection, vendor intent, multi-intent coverage, two tiers."""

import json

import httpx2

from mcpilot import MCPilot, Policy
from mcpilot.catalog import Catalog
from mcpilot.discovery import FIND_TOOLS, DiscoveryTools
from mcpilot.integrations import github, notion
from mcpilot.models import Integration, ToolRule
from mcpilot.search import CapabilityIndex, _inflected, evaluate, normalize

META = "io.modelcontextprotocol.registry/official"


def entry(name, description, status="active"):
    return {"server": {"name": name, "version": "1.0.0", "description": description,
                       "remotes": [{"type": "streamable-http", "url": "https://secret-endpoint.example/mcp"}]},
            "_meta": {META: {"status": status}}}


REGISTRY = [
    entry("com.atlassian/atlassian-mcp-server", "Atlassian Rovo MCP server for Jira and Confluence"),
    entry("io.github.someone/jira-tool", "Jira tickets helper"),
    entry("io.github.other/confluence-cloud", "Confluence Cloud pages"),
    entry("eu.projektionisten/tourism", "Tourism data from projektionisten"),
    entry("de.fakturai/erechnung", "Faktura und Rechnung Werkzeuge"),
    entry("com.stripe/mcp", "Stripe payments, invoices and customers"),
    entry("ai.waystation/slack", "Slack channels and messages"),
    entry("io.github.x/old-slack", "Slack messages", status="deprecated"),
    entry("io.github.gone/slack-deleted", "Slack messages", status="deleted"),
]


async def catalog_with_registry(tmp_path, approved=()):
    catalog = Catalog(approved, cache_path=tmp_path / "cache.json")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json={"servers": REGISTRY}))) as client:
        await catalog.sync(client=client)
    return catalog


def test_polish_inflection_maps_only_real_declensions():
    names = {"jira", "gitlab", "stripe", "asana", "slack", "fakto", "datao", "bazi", "notion"}
    assert {w: _inflected(w, names) for w in ["jirze", "gitlabie", "stripie", "asanie", "slacka", "notionie"]} == {
        "jirze": "jira", "gitlabie": "gitlab", "stripie": "stripe", "asanie": "asana", "slacka": "slack",
        "notionie": "notion"}
    assert [_inflected(w, names) for w in ["faktury", "database", "bazy", "stripe", "lokalny"]] == [None] * 5
    assert normalize("Zgłoszenia w Jirze, łączność!") == ["zgloszenia", "jirze", "lacznosc"]


async def test_vendor_intent_inflection_and_multi_intent_coverage(tmp_path):
    index = CapabilityIndex.from_catalog(await catalog_with_registry(tmp_path, [notion(), github()]))
    hits = index.search("porównaj tickety w jirze z dokumentacją w confluence", limit=4)
    assert hits[0].id == "com.atlassian/atlassian-mcp-server"  # named product → vendor's own server
    assert "eu.projektionisten/tourism" not in [h.id for h in hits]  # 'projektu' is not a vendor
    assert index.search("sprawdź faktury i płatności klientów w stripie")[0].id == "com.stripe/mcp"
    slack = [h.id for h in index.search("wyślij podsumowanie na kanał slacka", tier="registry")]
    assert slack[0] == "ai.waystation/slack" and "io.github.gone/slack-deleted" not in slack
    assert slack.index("io.github.x/old-slack") > 0  # deprecated ranks below active
    flagship = index.search("Znajdź dokumentację projektu w Notion i porównaj z issue w GitHub", limit=3)
    assert {"com.notion/mcp", "com.github/remote"} <= {h.id for h in flagship}
    assert all(h.tier == "approved" for h in flagship[:2])
    public = json.dumps([h.public() for h in index.search("slack jira stripe confluence", limit=10)])
    assert "secret-endpoint" not in public and "url" not in public


async def test_evaluate_reports_hit_rates(tmp_path):
    index = CapabilityIndex.from_catalog(await catalog_with_registry(tmp_path))
    report = evaluate(index, [{"query": "tickety w jirze", "expect_any": ["com.atlassian/atlassian-mcp-server"]},
                              {"query": "kanał slacka", "expect_any": ["~slack"]},
                              {"query": "pogoda w Krakowie", "expect_any": ["~weather"]}])
    assert (report["hit@1"], report["hit@3"]) == (0.667, 0.667) and len(report["misses"]) == 1


async def test_discovery_uses_context_search_for_unaliased_services_and_suggests_registry(tmp_path):
    jira = Integration(id="com.atlassian/jira", service="jira", publisher="Atlassian", version="1",
                       transport="stdio", command=("python", "-c", "pass"), support="supported",
                       capabilities=("tickets.read",), tools={"search_tickets": ToolRule(capability="tickets.read")})
    catalog = await catalog_with_registry(tmp_path, [jira])
    policy = Policy([jira], capabilities=["tickets.read"])
    async with MCPilot(user_id="u", catalog=catalog, policy=policy, state_dir=tmp_path) as pilot:
        tools = DiscoveryTools(pilot, index=CapabilityIndex.from_catalog(catalog))
        plan = await pilot.plan("pokaż otwarte tickety w jirze")
        assert not plan.selections  # the router's aliases do not know Jira
        found = await tools.handle(FIND_TOOLS, {"task": "pokaż otwarte tickety", "context": "pracujemy w jirze"})
        assert [c["integration_id"] for c in found["connections"]] == ["com.atlassian/jira"]  # resolved by search
        assert found["suggestions"] == []
        found = await tools.handle(FIND_TOOLS, {"task": "przeszukaj kanały slacka"})
        assert found["tools"] == [] and found["suggestions"][0]["id"] == "ai.waystation/slack"
        assert found["suggestions"][0]["action"] == "needs_admin_approval"
        assert "secret-endpoint" not in json.dumps(found)
