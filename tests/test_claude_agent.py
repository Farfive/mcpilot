"""Agent loop against a scripted Messages API; no credential or network to Anthropic."""

from __future__ import annotations

import json

import httpx2
import pytest

anthropic = pytest.importorskip("anthropic")

from examples.claude_agent import MODEL, run_flagship_agent  # noqa: E402
from examples.notion_github_task import GITHUB_PAT, SPEC, TASK  # noqa: E402


class ScriptedModel:
    """Plays the model: find tools, read issue #42 and the Notion spec, then answer."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.headers: list[httpx2.Headers] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(request.headers)
        turn = len(self.requests)
        last = body["messages"][-1]["content"]
        result = json.loads(last[0]["content"]) if isinstance(last, list) else {}
        if turn == 1:
            return self.tool_use("mcpilot_find_tools", {"task": TASK})
        if turn == 2:
            assert result["needs_user"] == [], result
            ids = {tool["name"]: tool["id"] for tool in result["tools"]}
            return self.tool_use("mcpilot_call_tool", [
                {"tool_id": ids["issue_read"], "arguments": {"method": "get", "owner": "acme", "repo": "atlas", "issue_number": 42}},
                {"tool_id": ids["notion-fetch"], "arguments": {"id": "page-atlas-spec"}},
            ])
        texts = [json.loads(block["content"])["content"][0]["text"] for block in last]
        return self.message([{"type": "text", "text": "Szkic podsumowania:\n" + "\n---\n".join(texts)}], "end_turn")

    def tool_use(self, name: str, inputs: dict | list[dict]) -> httpx2.Response:
        calls = inputs if isinstance(inputs, list) else [inputs]
        return self.message([{"type": "tool_use", "id": f"toolu_{len(self.requests)}_{i}", "name": name,
                              "input": arguments} for i, arguments in enumerate(calls)], "tool_use")

    def message(self, content: list[dict], stop_reason: str) -> httpx2.Response:
        return httpx2.Response(200, json={
            "id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant", "model": MODEL,
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })


async def test_claude_agent_loop_uses_meta_tools_and_never_sees_secrets():
    model = ScriptedModel()
    client = anthropic.AsyncAnthropic(
        api_key="test-key", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(model)),
    )
    async with client:
        answer = await run_flagship_agent(client)
    assert "Resume indexing after authentication" in answer and SPEC.splitlines()[0] in answer
    first = model.requests[0]
    assert first["model"] == MODEL and first["fallbacks"] == "default"
    assert first["output_config"] == {"effort": "medium"}
    assert [tool["name"] for tool in first["tools"]] == ["mcpilot_find_tools", "mcpilot_call_tool"]
    assert "server-side-fallback-2026-07-01" in model.headers[0]["anthropic-beta"]
    sent = json.dumps(model.requests)
    for secret in (GITHUB_PAT, "fixture-access-token", "fixture-refresh-token", "/authorize", "code_challenge"):
        assert secret not in sent
    assert "127.0.0.1" not in sent  # endpoints stay inside the SDK
