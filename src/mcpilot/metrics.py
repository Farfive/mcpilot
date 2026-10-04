"""Pilot metrics from the audit log (docs/product.md), computed only from secret-free events.

Run: python -m mcpilot.metrics audit.jsonl [--json]

Derivable: time to first successful read, resume-after-login rate, connection
success, latency. Not derivable from MCPilot data and reported as null:
selection accuracy (needs labelled tasks) and account upkeep cost.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

# Connect outcomes that wait for a person (user login or administrator setup), not failures.
_NEEDS_PERSON = {"auth_required", "setup_required"}


def _time(event: dict[str, Any]) -> float:
    return datetime.fromisoformat(event["at"]).timestamp()


def _percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(share * len(ordered)) - 1)], 3)


def _summary(values: list[float]) -> dict[str, Any]:
    return {"count": len(values), "p50": _percentile(values, 0.5), "p90": _percentile(values, 0.9),
            "p95": _percentile(values, 0.95)}


def pilot_metrics(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(events, key=_time)
    by_account: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for event in ordered:
        if event.get("integration_id"):
            by_account[(event.get("tenant"), event["user_id"], event["integration_id"],
                        event.get("account") or "default")].append(event)

    first_read: list[float] = []
    required = resumed = 0
    for stream in by_account.values():
        connects = [e for e in stream if e["action"] == "connect"]
        read = next((e for e in stream if e["action"] == "call" and e["outcome"] == "ok"
                     and e.get("effect") == "read"), None)
        if connects and read and _time(read) >= _time(connects[0]):
            first_read.append(round(_time(read) - _time(connects[0]), 3))
        waiting = next((e for e in connects if e["outcome"] == "auth_required"), None)
        if waiting is not None:
            required += 1
            resumed += any(e["outcome"] == "ready" and _time(e) >= _time(waiting) for e in connects)

    connect_outcomes = Counter(e["outcome"] for e in ordered if e["action"] == "connect")
    attempts = sum(n for outcome, n in connect_outcomes.items() if outcome not in _NEEDS_PERSON)
    latency: dict[str, list[float]] = defaultdict(list)
    for event in ordered:
        if event["action"] == "call" and event["outcome"] == "ok" and event.get("duration_ms") is not None:
            latency[event["integration_id"]].append(event["duration_ms"])
    return {
        "events": len(ordered),
        "time_to_first_successful_read_s": _summary(first_read),
        "resume_after_login": {"required": required, "resumed": resumed,
                               "rate": round(resumed / required, 3) if required else None},
        "connection_success": {"outcomes": dict(connect_outcomes), "attempts_excluding_waiting_for_person": attempts,
                               "rate": round(connect_outcomes["ready"] / attempts, 3) if attempts else None},
        "call_latency_ms": {"all": _summary([v for values in latency.values() for v in values]),
                            **{integration: _summary(values) for integration, values in sorted(latency.items())}},
        "selection_accuracy": {"value": None, "reason": "requires labelled tasks; not recorded by MCPilot"},
        "account_upkeep_cost": {"value": None, "reason": "no cost data in the SDK or audit events"},
    }


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mcpilot.metrics")
    parser.add_argument("audit_log", help="JSON Lines file written by JsonlAuditSink")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    report = pilot_metrics(read_jsonl(args.audit_log))
    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return
    first = report["time_to_first_successful_read_s"]
    resume = report["resume_after_login"]
    success = report["connection_success"]
    latency = report["call_latency_ms"]["all"]
    print(f"Zdarzenia: {report['events']}")
    print(f"Czas do pierwszego skutecznego odczytu [s]: n={first['count']} p50={first['p50']} p90={first['p90']}")
    print(f"Wznowienia po logowaniu: {resume['resumed']}/{resume['required']} (rate={resume['rate']})")
    print(f"Skuteczność połączeń: rate={success['rate']} {success['outcomes']}")
    print(f"Opóźnienie wywołań [ms]: n={latency['count']} p50={latency['p50']} p95={latency['p95']}")
    print("Trafność wyboru: brak danych (wymaga oznaczonych zadań)")
    print("Koszt utrzymania aktywnego konta: brak danych")


if __name__ == "__main__":
    main()
