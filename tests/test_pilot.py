"""TECH-2: the pilot protocol runner against loopback provider fixtures (real OAuth/MCP, simulated providers)."""

import json

import httpx2
from cryptography.fernet import Fernet

from examples.notion_github_task import GITHUB_PAT, github_fixture, notion_fixture
from mcpilot.integrations import github, notion
from mcpilot.pilot import main, render_markdown, run_pilot


def browser_for(fixture):
    """The user's browser: consent at the provider, then the redirect to the loopback callback."""

    def open_url(url: str) -> None:
        with httpx2.Client(follow_redirects=False) as browser:
            consent = browser.get(url + "&fixture_consent=allow")
            assert consent.status_code == 302
            assert browser.get(consent.headers["location"]).status_code == 200

    return open_url


async def test_notion_oauth_protocol_passes_every_automatable_step(tmp_path):
    with notion_fixture().serve() as fixture:
        report = await run_pilot(
            integration=notion(fixture.endpoint), tool="notion-search", arguments={"query": "Project Atlas"},
            state_dir=tmp_path, key=Fernet.generate_key(), login_port=0, opener=browser_for(fixture),
            allow_loopback=True)
        assert fixture.refresh_count == 1
    steps = {s["name"]: s["status"] for s in report["steps"]}
    assert steps == {"logowanie i połączenie": "pass", "mapowanie narzędzi (tools/list)": "pass", "odczyt": "pass",
                     "ponowne użycie po restarcie": "pass", "odświeżenie tokenu": "pass",
                     "cofnięcie u dostawcy": "skipped", "cofnięcie lokalne": "pass"}
    assert report["verdict"] == "pass_without_provider_revoke"
    assert report["evidence"] == "Demonstracyjne (lokalny fixture)"
    assert report["server_tools"] == ["notion-fetch", "notion-search"]
    text = render_markdown(report) + json.dumps(report)
    for leaked in ("fixture-access-token", "fixture-refresh-token", "Project Atlas", "code_challenge", "state="):
        assert leaked not in text


async def test_github_pat_protocol_with_provider_revocation(tmp_path):
    with github_fixture().serve() as fixture:
        def revoke_at_provider(_message: str) -> None:
            fixture.static_tokens = ()  # the user deletes the PAT in GitHub settings

        report = await run_pilot(
            integration=github(fixture.endpoint), tool="search_issues", arguments={"query": "repo:acme/atlas"},
            state_dir=tmp_path, key=Fernet.generate_key(), secret=GITHUB_PAT, allow_loopback=True,
            interactive=True, prompt=revoke_at_provider)
    steps = {s["name"]: s["status"] for s in report["steps"]}
    assert steps["odświeżenie tokenu"] == "n/a" and steps["cofnięcie u dostawcy"] == "pass"
    assert report["verdict"] == "pass"
    assert GITHUB_PAT not in json.dumps(report)


async def test_renamed_server_tools_fail_the_mapping_step(tmp_path):
    with github_fixture().serve() as fixture:
        drifted = github(fixture.endpoint).model_copy(update={"tools": {
            **github(fixture.endpoint).tools, "search_issues_v2": github().tools["search_issues"]}})
        drifted = drifted.model_copy(update={"tools": {k: v for k, v in drifted.tools.items() if k != "search_issues"}})
        report = await run_pilot(
            integration=drifted, tool="search_issues_v2", arguments={"query": "x"}, state_dir=tmp_path,
            key=Fernet.generate_key(), secret=GITHUB_PAT, allow_loopback=True)
    mapping = next(s for s in report["steps"] if s["name"].startswith("mapowanie"))
    assert mapping["status"] == "fail" and "search_issues_v2" in mapping["detail"]
    assert report["verdict"] == "fail"


def test_cli_writes_redacted_reports(tmp_path, monkeypatch, capsys):
    with github_fixture().serve() as fixture:
        manifest = tmp_path / "approved.json"
        manifest.write_text(json.dumps([github(fixture.endpoint).model_dump(mode="json")]))
        monkeypatch.setenv("PILOT_PAT", GITHUB_PAT)
        try:
            main(["--manifest", str(manifest), "--integration", "com.github/remote", "--tool", "search_issues",
                  "--arguments", '{"query": "repo:acme/atlas"}', "--state-dir", str(tmp_path / "state"),
                  "--secret-env", "PILOT_PAT", "--allow-loopback", "--report", str(tmp_path / "report.md"),
                  "--json", str(tmp_path / "report.json")])
        except SystemExit as exc:
            assert exc.code == 0
    report = (tmp_path / "report.md").read_text()
    assert "pass_without_provider_revoke" in report and GITHUB_PAT not in report
    assert json.loads((tmp_path / "report.json").read_text())["provider"] == "github"
