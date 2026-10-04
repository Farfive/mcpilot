"""On-demand MCP connections with one lifecycle owner per session.

The public API is asyncio based. No process, network connection, directory or
background task is created by importing this module or constructing Runtime.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .auth import AuthRequired, AuthSetupRequired
from .installer import PackageInstaller, sanitized_environment
from .models import Integration


class RuntimeFailure(RuntimeError):
    """A sanitized connection/transport error; never includes remote payloads."""


class RuntimeTimeout(RuntimeFailure, TimeoutError):
    """An operation timed out. A tool may already have executed; do not replay it."""


def _sanitized_error(exc: BaseException, *, connecting: bool = False) -> Exception:
    # AnyIO task groups wrap callback exceptions. Preserve only our explicitly
    # safe auth states, never arbitrary exceptions with credentials or URLs.
    if isinstance(exc, (AuthRequired, AuthSetupRequired, RuntimeFailure)):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            safe = _sanitized_error(child, connecting=connecting)
            if isinstance(safe, (AuthRequired, AuthSetupRequired, RuntimeTimeout)):
                return safe
    if isinstance(exc, TimeoutError) or isinstance(exc, MCPError) and exc.code == -32001:
        return RuntimeTimeout(
            "MCP connection timed out" if connecting
            else "MCP operation timed out; execution outcome may be unknown"
        )
    return RuntimeFailure("MCP connection failed or closed" if connecting else "MCP operation failed")


@dataclass
class _Request:
    operation: str
    arguments: tuple[Any, ...]
    result: asyncio.Future[Any]


class Session:
    """A live connection. Enter and exit SDK cancel scopes in the same task."""

    def __init__(
        self,
        integration: Integration,
        *,
        command: tuple[str, ...],
        directory: Path,
        auth: Any,
        headers: dict[str, str] | None,
        env: dict[str, str] | None,
        timeout: float,
        connect_timeout: float,
        max_tools: int,
    ) -> None:
        self.integration = integration
        self.timeout = timeout
        self._command = command
        self._directory = directory
        self._auth = auth
        self._headers = headers
        self._env = env
        self._connect_timeout = connect_timeout
        self._max_tools = max_tools
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        return self._closed or self._task is None or self._task.done()

    async def _start(self) -> Session:
        self._task = asyncio.create_task(self._run(), name="mcpilot-session")
        try:
            await asyncio.shield(self._ready)
        except BaseException:
            await self.close()
            # Consume a failure if cancellation raced with connection setup.
            if self._ready.done() and not self._ready.cancelled():
                self._ready.exception()
            raise
        return self

    async def _run(self) -> None:
        pending: _Request | None = None
        try:
            async with AsyncExitStack() as stack:
                # asyncio.timeout does not open an AnyIO cancel scope around
                # __aenter__ only (that would break the SDK scope stack).
                async with asyncio.timeout(self._connect_timeout):
                    if self.integration.transport == "http":
                        client_http = await stack.enter_async_context(
                            httpx2.AsyncClient(
                                auth=self._auth,
                                headers=self._headers,
                                timeout=httpx2.Timeout(self.timeout),
                                follow_redirects=False,
                                trust_env=False,
                            )
                        )
                        transport = streamable_http_client(
                            str(self.integration.endpoint), http_client=client_http
                        )
                    else:
                        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                        errlog = stack.enter_context(open(os.devnull, "w"))
                        parameters = StdioServerParameters(
                            command=self._command[0],
                            args=list(self._command[1:]),
                            env=sanitized_environment(self._directory, self._env),
                            cwd=self._directory,
                        )
                        transport = stdio_client(parameters, errlog=errlog)
                    client = await stack.enter_async_context(
                        Client(transport, read_timeout_seconds=self.timeout, cache=None)
                    )
                self._ready.set_result(None)
                while True:
                    pending = await self._queue.get()
                    if pending is None:
                        break
                    if pending.result.cancelled():
                        pending = None
                        continue
                    try:
                        async with asyncio.timeout(self.timeout):
                            value = await self._execute(client, pending)
                        if not pending.result.done():
                            pending.result.set_result(value)
                    except Exception as exc:
                        if not pending.result.done():
                            pending.result.set_exception(_sanitized_error(exc))
                    finally:
                        pending = None
        except BaseException as exc:
            error = _sanitized_error(exc, connecting=True)
            if not self._ready.done():
                self._ready.set_exception(error)
            if pending is not None and not pending.result.done():
                pending.result.set_exception(error)
        finally:
            self._closed = True
            while not self._queue.empty():
                queued = self._queue.get_nowait()
                if queued is not None and not queued.result.done():
                    queued.result.set_exception(RuntimeFailure("MCP session is closed"))
            # Do not retain credentials after connection teardown.
            self._auth = None
            self._headers = None
            self._env = None

    async def _execute(self, client: Client, request: _Request) -> Any:
        if request.operation == "ping":
            # Protocol 2026-07-28 removed ping (deprecated in legacy mode too); a
            # read-only discovery request verifies liveness without running a tool.
            await client.list_tools()
            return None
        if request.operation == "list_tools":
            tools: list[dict[str, Any]] = []
            cursor: str | None = None
            seen: set[str] = set()
            names: set[str] = set()
            for _ in range(100):
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:
                    if tool.name in names:
                        raise RuntimeFailure("MCP server returned duplicate tool names")
                    names.add(tool.name)
                    tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.input_schema,
                        "output_schema": tool.output_schema,
                        "annotations": tool.annotations.model_dump() if tool.annotations else {},
                    })
                    if len(tools) > self._max_tools:
                        raise RuntimeFailure("MCP tool listing exceeds limit")
                cursor = page.next_cursor
                if cursor is None:
                    return tools
                if cursor in seen:
                    raise RuntimeFailure("MCP pagination cursor repeated")
                seen.add(cursor)
            raise RuntimeFailure("MCP pagination exceeds limit")
        if request.operation == "call_tool":
            result = await client.call_tool(
                request.arguments[0], request.arguments[1], read_timeout_seconds=self.timeout
            )
            return {
                "content": [block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in result.content],
                "structured_content": result.structured_content,
                "is_error": result.is_error,
            }
        raise RuntimeFailure("Unsupported MCP operation")

    async def _request(self, operation: str, *arguments: Any) -> Any:
        if self.closed:
            raise RuntimeFailure("MCP session is closed")
        result = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Request(operation, arguments, result))
        return await result

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._request("list_tools")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("call_tool", name, arguments)

    async def ping(self) -> None:
        await self._request("ping")

    async def close(self) -> None:
        if self._task is None:
            self._closed = True
            return
        if not self._closed:
            self._closed = True
            while not self._queue.empty():
                queued = self._queue.get_nowait()
                if queued is not None and not queued.result.done():
                    queued.result.set_exception(RuntimeFailure("MCP session is closed"))
            self._queue.put_nowait(None)
        # The worker exits all transport/session contexts in its own task.
        # In-flight calls already have bounded timeouts; do not cancel or replay.
        await asyncio.shield(self._task)

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


class Runtime:
    """Manage approved sessions. Authorization/policy checks belong to the SDK host."""

    def __init__(
        self,
        directory: Path | str,
        *,
        timeout: float = 30.0,
        connect_timeout: float = 30.0,
        max_tools: int = 500,
    ) -> None:
        if timeout <= 0 or connect_timeout <= 0 or max_tools < 1:
            raise ValueError("Runtime limits must be positive")
        self.directory = Path(directory).expanduser().resolve()
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_tools = max_tools
        self.installer = PackageInstaller(self.directory / "packages")
        self._sessions: set[Session] = set()
        self._closed = False

    async def open(
        self,
        integration: Integration,
        *,
        auth: Any = None,
        headers: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> Session:
        if self._closed:
            raise RuntimeFailure("MCP runtime is closed")
        command = tuple(integration.command)
        if integration.transport == "stdio":
            if integration.package is not None:
                command = await self.installer.prepare(integration.package)
            if not command or not command[0]:
                raise RuntimeFailure("Local MCP integration has no launch command")
        elif integration.transport == "http":
            if not integration.endpoint:
                raise RuntimeFailure("Remote MCP integration has no endpoint")
        else:
            raise RuntimeFailure("Unsupported MCP transport")
        if self._closed:
            raise RuntimeFailure("MCP runtime closed during package installation")
        session = Session(
            integration,
            command=command,
            directory=self.directory / "processes" / secrets.token_hex(16),
            auth=auth,
            headers=dict(headers) if headers else None,
            env=dict(env) if env else None,
            timeout=self.timeout,
            connect_timeout=self.connect_timeout,
            max_tools=self.max_tools,
        )
        self._sessions.add(session)
        try:
            return await session._start()
        except BaseException:
            self._sessions.discard(session)
            raise

    async def close(self) -> None:
        self._closed = True
        sessions, self._sessions = self._sessions, set()
        await asyncio.gather(*(session.close() for session in sessions))

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
