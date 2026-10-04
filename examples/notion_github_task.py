"""Flagship scenario: Notion documentation vs GitHub issues -> draft summary.

Run from the repository: python -m examples.notion_github_task

DEMONSTRATIVE. Real: Streamable HTTP, MCP initialize/tools/list/tools/call,
OAuth discovery + DCR + PKCE via the official SDK, encrypted token storage,
durable workflow state and resume. Simulated: Notion and GitHub themselves
(loopback fixtures using the providers' tool names), the user's consent click,
and the summary text, which a host model would write.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from examples.oauth_fixture import FixtureTool, OAuthFixture
from mcpilot import MCPilot, Policy
from mcpilot.auth import (
    AuthManager,
    AuthorizationRequest,
    EncryptedFileSecretStore,
    LoginBroker,
    OAuthConfig,
)
from mcpilot.catalog import Catalog
from mcpilot.integrations import github, notion
from mcpilot.sdk import AuditSink
from mcpilot.workflow import WorkflowRun, WorkflowRunner, WorkflowStep, WorkflowStore

TASK = "Znajdź dokumentację projektu w Notion, porównaj ją z issue w GitHub i przygotuj podsumowanie"
USER_ID = "user-123"  # Authenticated by the host application, never chosen by the model.
GITHUB_PAT = "ghp_fixture_personal_access_token"

SPEC = (
    "Project Atlas - specyfikacja\n"
    "- Indeksowanie przyrostowe dokumentów Markdown.\n"
    "- Po logowaniu OAuth indeksowanie wznawia się od ostatniego punktu kontrolnego.\n"
    "- Załączniki PDF są poza zakresem MVP."
)
ISSUES = {
    42: ("Resume indexing after authentication", "open",
         "After the OAuth login the indexer restarts from zero instead of the last checkpoint."),
    57: ("Support PDF attachments", "open", "Users ask for PDF indexing in project spaces."),
}


def _schema(**properties: str) -> dict:
    return {"type": "object", "properties": {k: {"type": v} for k, v in properties.items()},
            "required": list(properties)}


def notion_fixture() -> OAuthFixture:
    def search(arguments: dict) -> str:
        found = "atlas" in arguments["query"].lower()
        return json.dumps([{"id": "page-atlas-spec", "title": "Project Atlas - specyfikacja"}] if found else [])

    return OAuthFixture(expected_scope=None, tools={
        "notion-search": FixtureTool("Search pages in the connected Notion workspace",
                                     _schema(query="string"), search),
        "notion-fetch": FixtureTool("Fetch a Notion page by id", _schema(id="string"),
                                    lambda a: SPEC if a["id"] == "page-atlas-spec" else "Not found"),
    })


def github_fixture() -> OAuthFixture:
    def search(_: dict) -> str:
        return "\n".join(f"#{n} {title} ({state})" for n, (title, state, _) in ISSUES.items())

    def read(arguments: dict) -> str:
        title, state, body = ISSUES[arguments["issue_number"]]
        return f"#{arguments['issue_number']} {title}\nState: {state}\n{body}"

    return OAuthFixture(static_tokens=(GITHUB_PAT,), tools={
        "search_issues": FixtureTool("Search issues", _schema(query="string"), search),
        # Mirrors github-mcp-server v1.14.0: issue_read requires a `method` (spec/providers snapshot).
        "issue_read": FixtureTool("Read one issue", {
            **_schema(method="string", owner="string", repo="string", issue_number="integer"),
            "properties": {"method": {"type": "string", "enum": ["get", "get_comments", "get_sub_issues",
                                                                  "get_parent", "get_labels"]},
                           "owner": {"type": "string"}, "repo": {"type": "string"},
                           "issue_number": {"type": "integer"}}}, read),
    })


def steps_for(plan_ids: set[str]) -> list[WorkflowStep]:
    """What a host agent would decide after tools_for(); scripted so the demo needs no model key."""
    assert plan_ids == {"com.github/remote", "com.notion/mcp"}, plan_ids
    return [
        WorkflowStep(integration_id="com.github/remote", capability="issues.read", tool_name="search_issues",
                     arguments={"query": "repo:acme/atlas is:issue is:open indexing"}),
        WorkflowStep(integration_id="com.github/remote", capability="issues.read", tool_name="issue_read",
                     arguments={"method": "get", "owner": "acme", "repo": "atlas", "issue_number": 42}),
        WorkflowStep(integration_id="com.notion/mcp", capability="documents.search", tool_name="notion-search",
                     arguments={"query": "Project Atlas"}),
        WorkflowStep(integration_id="com.notion/mcp", capability="documents.search", tool_name="notion-fetch",
                     arguments={"id": "page-atlas-spec"}),
    ]


def draft_summary(run: WorkflowRun) -> dict:
    """Stand-in for the host model. Preparing a message is local; nothing is sent."""
    text = {s.step.tool_name: "\n".join(b.get("text", "") for b in s.result.content)
            for s in run.steps if s.result is not None}
    body = (
        "Podsumowanie (szkic)\n\n"
        f"Dokumentacja (Notion):\n{text['notion-fetch']}\n\n"
        f"Issue (GitHub):\n{text['issue_read']}\n\n"
        f"Pozostałe otwarte issue:\n{text['search_issues']}"
    )
    return {"status": "draft", "sent": False, "body": body}


async def run_scenario(state_dir: Path, audit: AuditSink | None = None) -> dict:
    key = Fernet.generate_key()  # A deployed host loads this from its secret manager.
    credentials = state_dir / "credentials"
    with notion_fixture().serve() as notion_fx, github_fixture().serve() as github_fx:
        catalog = Catalog([notion(notion_fx.endpoint), github(github_fx.endpoint)])
        policy = Policy(catalog.all(), capabilities=("documents.search", "issues.read"), allow_loopback=True)
        store = WorkflowStore(state_dir / "workflows.sqlite")

        # Earlier session: the user connected GitHub with a PAT through the host UI.
        auth = AuthManager(EncryptedFileSecretStore(credentials, key))
        await auth.set_secret(USER_ID, "com.github/remote", "default", GITHUB_PAT, endpoint=github_fx.endpoint)

        async with MCPilot(user_id=USER_ID, catalog=catalog, policy=policy, auth=auth,
                           state_dir=state_dir, audit=audit) as pilot:
            plan = await pilot.plan(TASK)
            discovery = await pilot.discover(TASK)
            runner = WorkflowRunner(pilot, store)
            run_id = await runner.create(steps_for({s.integration_id for s in plan.selections}), task=TASK)

            # 1. No login UI is attached yet: GitHub steps run, Notion waits.
            first = await runner.resume(run_id)
            statuses = {c.integration_id: c.status for c in await pilot.status()}

            # 2. The user's browser session is open: show a "Connect Notion" button.
            buttons: asyncio.Queue[AuthorizationRequest] = asyncio.Queue()
            broker = LoginBroker(buttons.put, timeout=30)
            auth.configure_oauth("com.notion/mcp", OAuthConfig(login=broker))
            resumed = asyncio.create_task(runner.resume(run_id))
            button = await asyncio.wait_for(buttons.get(), timeout=10)
            assert broker.pending(USER_ID) == (button,)
            # The user clicks, consents at the provider and is redirected to the host route.
            callback_url = await notion_fx.approve(button.url)
            assert broker.complete_url(user_id=USER_ID, callback_url=callback_url)
            final = await resumed
            assert button.url not in final.model_dump_json()
            summary = draft_summary(final)

        # 3. Process restart, new task: both accounts are reused, no login UI configured.
        restarted = AuthManager(EncryptedFileSecretStore(credentials, key))
        async with MCPilot(user_id=USER_ID, catalog=catalog, policy=policy, auth=restarted,
                           state_dir=state_dir, audit=audit) as pilot:
            again = await pilot.tools_for("Sprawdź issue #42 w GitHub i stronę projektu w Notion")
            reader = next(t for t in again.tools if t.name == "notion-fetch")
            reread = await pilot.call(reader.id, {"id": "page-atlas-spec"})

        return {
            "label": "demonstracyjne: prawdziwe MCP/HTTP/OAuth, symulowani dostawcy",
            "plan": sorted((s.integration_id, s.capability) for s in plan.selections),
            "discovery": discovery,
            "first_resume": first.status,
            "steps_after_first_resume": [s.status for s in first.steps],
            "connections_before_login": statuses,
            "login_button": {"integration": button.integration_id, "connection_id": button.connection_id},
            "final_status": final.status,
            "github_calls": len(github_fx.calls),
            "notion_logins": notion_fx.authorization_count,
            "after_restart": sorted((c.integration_id, c.status) for c in again.connections),
            "reread_ok": SPEC in reread.content[0]["text"],
            "summary": summary,
        }


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mcpilot-flagship-") as directory:
        report = await run_scenario(Path(directory))
    body = report.pop("summary")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print("\n" + body["body"])


if __name__ == "__main__":
    asyncio.run(main())
