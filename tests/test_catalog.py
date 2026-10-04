from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx2 as httpx
import pytest

from mcpilot.catalog import OFFICIAL_REGISTRY, Catalog
from mcpilot.models import Integration, ToolRule


def entry(name="io.example/server", *, status="active", description="Search documents"):
    return {
        "server": {
            "name": name,
            "version": "1.2.3",
            "description": description,
            "remotes": [{"type": "streamable-http", "url": "https://evil.example/mcp"}],
            "packages": [{"identifier": "malicious-package", "runtimeArguments": ["--evil"]}],
        },
        "_meta": {"io.modelcontextprotocol.registry/official": {"status": status}},
    }


def trusted():
    return Integration(
        id="io.example/server", service="example", publisher="Example", version="1.0.0",
        transport="http", endpoint="https://approved.example/mcp", support="supported",
        capabilities=("documents.read",), tools={"read": ToolRule(capability="documents.read")},
    )


async def test_registry_is_informational_paginated_and_offline_cache(tmp_path):
    requests = []

    def serve(request):
        requests.append(request)
        assert request.url.params["version"] == "latest"
        assert request.url.params["include_deleted"] == "true"
        if not request.url.params.get("cursor"):
            return httpx.Response(200, json={
                "servers": [entry(description="hello\x00world" + "!" * 900)],
                "metadata": {"nextCursor": "opaque+/cursor="},
            })
        assert request.url.params["cursor"] == "opaque+/cursor="
        return httpx.Response(200, json={"servers": [entry("io.example/second")]})

    path = tmp_path / "nested" / "catalog.json"
    catalog = Catalog([trusted()], cache_path=path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        status = await catalog.sync(client=client)
    assert len(requests) == 2
    assert status[OFFICIAL_REGISTRY]["error"] is None
    assert status[OFFICIAL_REGISTRY]["last_sync"]
    assert catalog.get("io.example/server").endpoint == "https://approved.example/mcp"
    with pytest.raises(KeyError):
        catalog.get("io.example/second")
    found = catalog.discovered()
    assert len(found) == 2 and found[0]["untrusted"] is True
    assert len(found[1]["description"]) <= 500
    assert "\x00" not in json.dumps(found)
    assert "evil.example" not in path.read_text()
    assert "malicious-package" not in path.read_text()
    assert Catalog(cache_path=path).discovered() == found
    assert Catalog(cache_path=path).all() == ()


async def test_snapshot_reconciles_deletions_and_retains_cache_on_partial_failure(tmp_path):
    path = tmp_path / "catalog.json"
    catalog = Catalog(cache_path=path)

    async def sync(handler):
        # full_refresh_after=0: every sync is a full snapshot that reconciles deletions.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await catalog.sync(client=client, full_refresh_after=0)

    await sync(lambda _: httpx.Response(200, json={"servers": [entry("io.example/old")]}))
    initial_sync = catalog.sync_status()[OFFICIAL_REGISTRY]["last_sync"]

    def fails_second_page(request):
        if request.url.params.get("cursor"):
            return httpx.Response(503, text="upstream failure token=secret")
        return httpx.Response(200, json={
            "servers": [entry("io.example/new")], "metadata": {"nextCursor": "next"},
        })

    status = await sync(fails_second_page)
    assert catalog.discovered()[0]["id"] == "io.example/old"
    assert status[OFFICIAL_REGISTRY]["last_sync"] == initial_sync
    assert status[OFFICIAL_REGISTRY]["error"] == "HTTPStatusError"
    assert "secret" not in path.read_text()
    assert Catalog(cache_path=path).discovered() == catalog.discovered()

    await sync(lambda _: httpx.Response(200, json={"servers": [
        entry("io.example/old", status="deleted"), entry("io.example/new"),
    ]}))
    assert [s["id"] for s in catalog.discovered()] == ["io.example/new"]
    await sync(lambda _: httpx.Response(200, json={"servers": []}))
    assert catalog.discovered() == ()


async def test_incremental_sync_merges_updates_and_periodic_full_refresh_reconciles(tmp_path):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if "updated_since" not in request.url.params:
            return httpx.Response(200, json={"servers": [entry("io.example/a"), entry("io.example/b")]})
        return httpx.Response(200, json={"servers": [
            entry("io.example/b", status="deleted"), entry("io.example/c", description="new")]})

    path = tmp_path / "catalog.json"
    catalog = Catalog(cache_path=path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await catalog.sync(client=client)
        assert "updated_since" not in seen[-1]
        first = catalog.sync_status()[OFFICIAL_REGISTRY]
        assert first["last_full_sync"] == first["last_sync"]

        # A restarted process continues incrementally from the cached timestamps.
        restarted = Catalog(cache_path=path)
        await restarted.sync(client=client)
        since = datetime.fromisoformat(seen[-1]["updated_since"].replace("Z", "+00:00"))
        last = datetime.fromisoformat(first["last_sync"].replace("Z", "+00:00"))
        assert last - since == timedelta(minutes=5)
        assert [s["id"] for s in restarted.discovered()] == ["io.example/a", "io.example/c"]
        status = restarted.sync_status()[OFFICIAL_REGISTRY]
        assert status["last_full_sync"] == first["last_full_sync"] != status["last_sync"]

        await restarted.sync(client=client, full_refresh_after=0)
        assert "updated_since" not in seen[-1]
        assert [s["id"] for s in restarted.discovered()] == ["io.example/a", "io.example/b"]


async def test_repeating_cursor_and_page_bound_never_commit_partial_snapshot():
    def handler(request):
        name = "io.example/second" if request.url.params.get("cursor") else "io.example/first"
        return httpx.Response(200, json={
            "servers": [entry(name)], "metadata": {"nextCursor": "same"},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = Catalog()
        state = await catalog.sync(client=client)
        assert state[OFFICIAL_REGISTRY]["error"] == "ValueError"
        assert catalog.discovered() == ()
        state = await catalog.sync(client=client, max_pages=1)
        assert state[OFFICIAL_REGISTRY]["error"] == "ValueError"
        assert catalog.discovered() == ()


def test_trusted_manifest_validation_is_atomic_and_defensive(tmp_path):
    original = trusted()
    catalog = Catalog([original])
    original.tools.clear()
    assert catalog.get(original.id).tools
    catalog.get(original.id).tools.clear()
    catalog.all()[0].tools.clear()
    assert catalog.get(original.id).tools
    path = tmp_path / "manifest.json"
    different = trusted().model_copy(update={"id": "second"})
    path.write_text(json.dumps([different.model_dump(), {"id": "broken"}]))
    with pytest.raises(ValueError):
        catalog.load_manifest(path)
    assert len(catalog.all()) == 1
    path.write_text(json.dumps({"integrations": [different.model_dump()]}))
    loaded = catalog.load_manifest(path)
    loaded[0].tools.clear()
    assert catalog.get("second").tools


async def test_sources_stay_separate_and_redirects_are_not_followed():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "private.example":
            return httpx.Response(302, headers={"location": "https://other.example/steal"})
        return httpx.Response(200, json={"servers": [entry()]})

    catalog = Catalog()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        state = await catalog.sync((OFFICIAL_REGISTRY, "https://private.example"), client=client)
    assert len(requests) == 2
    assert state[OFFICIAL_REGISTRY]["error"] is None
    assert state["https://private.example/v0.1/servers"]["error"] == "HTTPStatusError"
    assert len(catalog.discovered()) == 1


async def test_background_sync_is_explicit_and_close_cancels():
    ready = asyncio.Event()

    def handler(_):
        ready.set()
        return httpx.Response(200, json={"servers": [entry()]})

    catalog = Catalog()
    assert catalog._sync_task is None
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = catalog.start_sync(3600, client=client)
        assert catalog.start_sync(3600, client=client) is task
        await asyncio.wait_for(ready.wait(), timeout=1)
        await catalog.close()
        assert task.done()
        assert not client.is_closed


def test_corrupt_cache_is_non_executable_and_reported(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text('{"format_version": 1, "sources": {"https://bad.example": {}}}')
    catalog = Catalog([trusted()], cache_path=path)
    assert catalog.cache_error == "invalid_cache"
    assert len(catalog.all()) == 1
    assert catalog.discovered() == ()


async def test_registry_urls_cannot_embed_secrets():
    with pytest.raises(ValueError):
        await Catalog().sync(("https://user:secret@example.com",))
    with pytest.raises(ValueError):
        await Catalog().sync(("https://example.com?token=secret",))


def test_invalid_background_settings_do_not_create_tasks():
    catalog = Catalog()
    for interval in (0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            catalog.start_sync(interval)
    with pytest.raises(ValueError):
        catalog.start_sync(max_pages=0)
    assert catalog._sync_task is None
