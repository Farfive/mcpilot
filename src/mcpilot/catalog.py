"""Trusted executable manifests and separate, non-executable registry discovery.

Registry v0.1: https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/generic-registry-api.md
Synchronization is explicit: a bounded full snapshot, then ``updated_since``
increments with an overlap window, and a periodic full refresh that reconciles
anything an increment cannot observe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import tempfile
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2 as httpx

from .models import Integration

OFFICIAL_REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"
_REGISTRY_META = "io.modelcontextprotocol.registry/official"
_MAX_PAGE_BYTES = 2_000_000
_MAX_CACHE_BYTES = 128_000_000  # official registry: ~40k entries, ~15 MB (2026-10)
_OVERLAP = timedelta(minutes=5)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _text(value: Any, limit: int) -> str:
    """Remove controls and bound text, without treating descriptions as instructions."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c for c in value[: limit * 2] if c.isprintable()).split())[:limit]


def _source_url(source: str) -> str:
    parsed = urlsplit(source)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
        )
    ):
        raise ValueError("Registry sources require HTTPS, no credentials/query/fragment")
    source = source.rstrip("/")
    if source.endswith("/v0.1/servers"):
        return source
    if source.endswith("/v0.1"):
        return source + "/servers"
    return source + "/v0.1/servers"


def _summary(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict) or not isinstance(entry.get("server"), dict):
        raise ValueError("Invalid registry server envelope")
    server = entry["server"]
    name = _text(server.get("name"), 256)
    version = _text(server.get("version"), 128)
    if not name or not version:
        raise ValueError("Registry entries require name and version")
    meta = entry.get("_meta", {})
    if not isinstance(meta, dict):
        raise ValueError("Invalid registry metadata")
    official = meta.get(_REGISTRY_META, {})
    if not isinstance(official, dict):
        raise ValueError("Invalid registry lifecycle metadata")
    status = official.get("status", "active")
    if status not in {"active", "deprecated", "deleted"}:
        raise ValueError("Unknown registry lifecycle status")
    transports: set[str] = set()
    for remote in server.get("remotes", []):
        if isinstance(remote, dict) and remote.get("type") in {"streamable-http", "sse"}:
            transports.add(remote["type"])
    for package in server.get("packages", []):
        if isinstance(package, dict):
            transport = package.get("transport", {})
            if isinstance(transport, dict) and transport.get("type") == "stdio":
                transports.add("stdio")
    return {
        "id": name,
        "version": version,
        "description": _text(server.get("description"), 500),
        "publisher_namespace": name.partition("/")[0],
        "transports": sorted(transports),
        "registry_status": status,
        "updated_at": _text(official.get("updatedAt", official.get("publishedAt")), 64),
        "support": "discovered",
        "untrusted": True,
    }


def _incremental_since(previous: dict[str, Any], now: datetime, full_refresh_after: float) -> str | None:
    """RFC3339 ``updated_since`` for an increment, or None when a full snapshot is due."""
    try:
        last = datetime.fromisoformat(previous["last_sync"].replace("Z", "+00:00"))
        full = datetime.fromisoformat(previous["last_full_sync"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        return None
    if (now - full).total_seconds() >= full_refresh_after or last > now:
        return None
    return (last - _OVERLAP).isoformat().replace("+00:00", "Z")


class Catalog:
    """Only ``add``/``load_manifest`` can create executable integrations.

    ``cache_path`` is a JSON file for untrusted discovery snapshots. Registry data
    never updates a trusted manifest, even when the IDs match. An injected HTTP
    client is owned by its caller; use an appropriately scoped client per private
    registry when it carries authentication headers.
    """

    def __init__(
        self, integrations: Iterable[Integration] = (), cache_path: Path | None = None
    ) -> None:
        self._trusted: dict[str, Integration] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.cache_error: str | None = None
        self._sync_lock = asyncio.Lock()
        self._sync_task: asyncio.Task[None] | None = None
        for integration in integrations:
            self.add(integration)
        self._load_cache()

    def add(self, integration: Integration) -> None:
        """Add or replace a manifest supplied by trusted host code."""
        self._trusted[integration.id] = integration.model_copy(deep=True)

    def get(self, integration_id: str) -> Integration:
        return self._trusted[integration_id].model_copy(deep=True)

    def all(self) -> tuple[Integration, ...]:
        return tuple(item.model_copy(deep=True) for item in self._trusted.values())

    def load_manifest(self, path: Path | str) -> list[Integration]:
        """Validate a host-owned JSON list (or ``{"integrations": [...]}``) atomically."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and set(raw) == {"integrations"}:
            raw = raw["integrations"]
        if not isinstance(raw, list):
            raise ValueError("A trusted manifest must contain a list of integrations")
        parsed = [Integration.model_validate(item) for item in raw]
        if len({item.id for item in parsed}) != len(parsed):
            raise ValueError("A manifest cannot contain duplicate integration IDs")
        for item in parsed:
            self.add(item)
        return parsed

    def discovered(self, limit: int = 20) -> tuple[dict[str, Any], ...]:
        """Bounded informational summaries, with no launch commands or endpoints."""
        if not 0 <= limit <= 1000:
            raise ValueError("Discovery limit must be between 0 and 1000")
        found = []
        for source, snapshot in sorted(self._snapshots.items()):
            for item in snapshot["servers"]:
                if item["registry_status"] != "deleted":
                    found.append({**item, "source": source})
        return tuple(deepcopy(found[:limit]))

    def sync_status(self) -> dict[str, dict[str, Any]]:
        return {
            source: {
                "last_sync": snapshot.get("last_sync"),
                "last_full_sync": snapshot.get("last_full_sync"),
                "last_attempt": snapshot.get("last_attempt"),
                "error": snapshot.get("error"),
                "count": len(snapshot["servers"]),
            }
            for source, snapshot in self._snapshots.items()
        }

    async def sync(
        self,
        sources: tuple[str, ...] = (OFFICIAL_REGISTRY,),
        *,
        client: httpx.AsyncClient | None = None,
        max_pages: int = 1000,
        page_size: int = 100,
        full_refresh_after: float = 86_400,
    ) -> dict[str, dict[str, Any]]:
        """Commit each source only after every page of the request arrived intact.

        The first sync (and any sync ``full_refresh_after`` seconds after the last
        full one) downloads a complete snapshot, which reconciles deletions and
        absent records. Other syncs fetch ``updated_since`` the previous sync
        minus an overlap window and merge by server name. A failed source keeps
        its previous snapshot and records an error code.
        """
        if not 1 <= max_pages <= 10_000 or not 1 <= page_size <= 100:
            raise ValueError("Invalid registry pagination limits")
        if not math.isfinite(full_refresh_after) or full_refresh_after < 0:
            raise ValueError("Invalid full refresh interval")
        normalized = tuple(dict.fromkeys(_source_url(source) for source in sources))
        async with self._sync_lock:
            if client is None:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as owned:
                    await self._sync_sources(normalized, owned, max_pages, page_size, full_refresh_after)
            else:
                await self._sync_sources(normalized, client, max_pages, page_size, full_refresh_after)
        return self.sync_status()

    async def _sync_sources(
        self, sources: tuple[str, ...], client: httpx.AsyncClient, max_pages: int, page_size: int,
        full_refresh_after: float,
    ) -> None:
        for source in sources:
            started = datetime.now(UTC)
            attempt = _timestamp()
            previous = self._snapshots.get(source, {"servers": [], "last_sync": None})
            since = _incremental_since(previous, started, full_refresh_after)
            try:
                fetched = await self._fetch_snapshot(source, client, max_pages, page_size, since)
                if since is None:
                    servers, full = fetched, attempt
                else:
                    merged = {item["id"]: item for item in previous["servers"]}
                    merged.update({item["id"]: item for item in fetched})
                    servers, full = [merged[key] for key in sorted(merged)], previous.get("last_full_sync")
                candidate = {
                    "servers": servers,
                    "last_sync": attempt,
                    "last_full_sync": full,
                    "last_attempt": attempt,
                    "error": None,
                }
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                # Exception messages may contain URLs, cursors or request headers.
                candidate = {
                    **previous,
                    "last_attempt": attempt,
                    "error": type(exc).__name__,
                }
            updated = {**self._snapshots, source: candidate}
            self._persist(updated)
            self._snapshots = updated

    async def _fetch_snapshot(
        self, source: str, client: httpx.AsyncClient, max_pages: int, page_size: int,
        updated_since: str | None = None,
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        servers: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(max_pages):
            params = {"version": "latest", "limit": str(page_size), "include_deleted": "true"}
            if updated_since:
                params["updated_since"] = updated_since
            if cursor:
                params["cursor"] = cursor
            async with client.stream(
                "GET", source, params=params, timeout=20, follow_redirects=False
            ) as response:
                response.raise_for_status()
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > _MAX_PAGE_BYTES:
                        raise ValueError("Registry page exceeds size limit")
                data = json.loads(payload)
            if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
                raise ValueError("Invalid registry list response")
            if len(data["servers"]) > page_size:
                raise ValueError("Registry returned more entries than requested")
            for entry in data["servers"]:
                item = _summary(entry)
                if item["id"] in servers:
                    raise ValueError("Registry snapshot contains duplicate latest IDs")
                servers[item["id"]] = item
            metadata = data.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("Invalid pagination metadata")
            cursor = metadata.get("nextCursor")
            if cursor is None or cursor == "":
                return [servers[key] for key in sorted(servers)]
            if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen:
                raise ValueError("Invalid or repeated registry cursor")
            seen.add(cursor)
        raise ValueError("Registry pagination exceeded max_pages; snapshot not committed")

    def start_sync(
        self,
        interval: float = 3600,
        *,
        sources: tuple[str, ...] = (OFFICIAL_REGISTRY,),
        client: httpx.AsyncClient | None = None,
        max_pages: int = 1000,
    ) -> asyncio.Task[None]:
        """Explicit opt-in; immediately sync, then wait between attempts."""
        if not math.isfinite(interval) or interval < 0.01:
            raise ValueError("Sync interval must be at least 0.01 seconds")
        if not 1 <= max_pages <= 10_000:
            raise ValueError("Invalid registry pagination limits")
        if self._sync_task is not None and not self._sync_task.done():
            return self._sync_task
        for source in sources:
            _source_url(source)

        async def run() -> None:
            while True:
                try:
                    await self.sync(sources, client=client, max_pages=max_pages)
                except (OSError, ValueError):
                    self.cache_error = "cache_write_failed"
                await asyncio.sleep(interval)

        self._sync_task = asyncio.create_task(run(), name="mcpilot-catalog-sync")
        return self._sync_task

    async def close(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task
            self._sync_task = None

    def _load_cache(self) -> None:
        if self.cache_path is None:
            return
        try:
            with self.cache_path.open("rb") as handle:
                payload = handle.read(_MAX_CACHE_BYTES + 1)
            if len(payload) > _MAX_CACHE_BYTES:
                raise ValueError("Registry cache exceeds size limit")
            data = json.loads(payload)
            if data.get("format_version") != 1 or not isinstance(data.get("sources"), dict):
                raise ValueError("Invalid registry cache")
            validated = {}
            for source, snapshot in data["sources"].items():
                if _source_url(source) != source or not isinstance(snapshot, dict):
                    raise ValueError("Invalid cached source")
                entries = snapshot.get("servers")
                if not isinstance(entries, list):
                    raise ValueError("Invalid cached servers")
                cleaned = []
                for entry in entries:
                    # Reconstruct through the same boundary; cache never adds launch data.
                    cleaned.append(_summary({
                        "server": {
                            "name": entry["id"],
                            "version": entry["version"],
                            "description": entry.get("description", ""),
                            "remotes": [{"type": t} for t in entry.get("transports", [])],
                            "packages": [{"transport": {"type": t}} for t in entry.get("transports", [])],
                        },
                        "_meta": {_REGISTRY_META: {
                            "status": entry["registry_status"],
                            "updatedAt": entry.get("updated_at", ""),
                        }},
                    }))
                validated[source] = {
                    "servers": cleaned,
                    "last_sync": _text(snapshot.get("last_sync"), 64) or None,
                    "last_full_sync": _text(snapshot.get("last_full_sync"), 64) or None,
                    "last_attempt": _text(snapshot.get("last_attempt"), 64) or None,
                    "error": _text(snapshot.get("error"), 128) or None,
                }
            self._snapshots = validated
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.cache_error = "invalid_cache"

    def _persist(self, snapshots: dict[str, dict[str, Any]]) -> None:
        if self.cache_path is None:
            return
        payload = json.dumps({"format_version": 1, "sources": snapshots}, ensure_ascii=False)
        if len(payload.encode()) > _MAX_CACHE_BYTES:
            raise ValueError("Registry cache exceeds size limit")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".mcpilot-catalog-", dir=self.cache_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.cache_path)
            self.cache_error = None
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
