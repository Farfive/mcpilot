"""BIZ-4: pilot metrics from the secret-free audit log of the flagship scenario."""

import json

from examples.notion_github_task import GITHUB_PAT, run_scenario
from mcpilot.audit import JsonlAuditSink
from mcpilot.metrics import main, pilot_metrics, read_jsonl


async def test_flagship_audit_log_yields_product_metrics(tmp_path, capsys):
    log = tmp_path / "audit.jsonl"
    await run_scenario(tmp_path / "state", audit=JsonlAuditSink(log))
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    raw = log.read_text()
    for secret in (GITHUB_PAT, "fixture-access-token", "authorize", "page-atlas-spec", "Project Atlas"):
        assert secret not in raw  # no credentials, URLs, arguments or results
    events = read_jsonl(log)
    assert {e["action"] for e in events} == {"connect", "call", "disconnect"}
    assert all(e["task_id"] for e in events if e["action"] == "call")

    report = pilot_metrics(events)
    assert report["resume_after_login"] == {"required": 1, "resumed": 1, "rate": 1.0}  # Notion login
    assert report["time_to_first_successful_read_s"]["count"] == 2  # GitHub and Notion
    assert report["connection_success"]["rate"] == 1.0
    assert report["call_latency_ms"]["all"]["count"] == 5  # 4 workflow steps + reread after restart
    assert set(report["call_latency_ms"]) == {"all", "com.github/remote", "com.notion/mcp"}
    assert report["selection_accuracy"]["value"] is None and report["account_upkeep_cost"]["value"] is None

    main([str(log), "--json"])
    assert json.loads(capsys.readouterr().out)["resume_after_login"]["rate"] == 1.0


def test_metrics_ignore_people_waits_and_handle_empty_logs():
    assert pilot_metrics([])["connection_success"]["rate"] is None
    events = [
        {"at": "2026-10-05T10:00:00+00:00", "user_id": "u", "action": "connect", "outcome": "setup_required",
         "integration_id": "com.slack/mcp", "account": "default"},
        {"at": "2026-10-05T10:00:01+00:00", "user_id": "u", "action": "connect", "outcome": "error",
         "integration_id": "com.github/remote", "account": "default"},
    ]
    report = pilot_metrics(events)
    assert report["connection_success"]["attempts_excluding_waiting_for_person"] == 1
    assert report["connection_success"]["rate"] == 0.0
