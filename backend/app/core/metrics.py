"""Prometheus text metrics, computed from the database at scrape time.

No in-memory counters: every value is a query over the evidence store, so the
numbers survive restarts and can never drift from the records they describe.
Served at ``GET /api/v1/metrics`` behind the same bearer token as every route.
"""

from __future__ import annotations

import json
import re

from backend.app.core.database import Database

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_BOUNDARY_FAILURE = re.compile(
    r"(APPCONTAINER_[A-Z_]+|LINUX_SANDBOX_UNAVAILABLE|LINUX_SANDBOX_VERIFICATION_FAILED|"
    r"LINUX_CGROUP_UNAVAILABLE|AGENT_RUNTIME_PROFILE_UNAVAILABLE)")
_LABEL_ESCAPE = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n"})


def _label(value: object) -> str:
    return str(value).translate(_LABEL_ESCAPE)


def _metric(lines: list[str], name: str, kind: str, help_text: str,
            samples: list[tuple[dict[str, object], float]]) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")
    for labels, value in samples:
        rendered = ",".join(f'{key}="{_label(item)}"' for key, item in sorted(labels.items()))
        lines.append(f"{name}{{{rendered}}} {value}" if rendered else f"{name} {value}")


def render_metrics(database: Database, *, eval_runs: int = 20) -> str:
    lines: list[str] = []
    with database.connection() as connection:
        changes = connection.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        runs = connection.execute(
            "SELECT status, COUNT(*) FROM agent_runs GROUP BY status").fetchall()
        error_payloads = [row[0] for row in connection.execute(
            "SELECT payload_json FROM agent_runs WHERE status = 'ERROR'")]
        checks = connection.execute(
            "SELECT state, COUNT(*) FROM check_runs GROUP BY state").fetchall()
        evals = connection.execute(
            "SELECT id, config_json, summary_json FROM eval_runs ORDER BY started_at DESC "
            "LIMIT ?", (eval_runs,)).fetchall()
        eval_total = connection.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0]
    _metric(lines, "sentinel_changes", "gauge", "Changes in the evidence store.", [({}, changes)])
    _metric(lines, "sentinel_agent_runs_total", "counter", "Recorded agent runs by status.",
            [({"status": status}, count) for status, count in runs] or [({"status": "PASSED"}, 0)])
    failures: dict[str, int] = {}
    for payload in error_payloads:
        match = _BOUNDARY_FAILURE.search(payload or "")
        if match:
            failures[match.group(1)] = failures.get(match.group(1), 0) + 1
    _metric(lines, "sentinel_boundary_failures_total", "counter",
            "Agent launches refused or failed at the boundary, by error code.",
            [({"code": code}, count) for code, count in sorted(failures.items())]
            or [({"code": "none"}, 0)])
    _metric(lines, "sentinel_check_runs", "gauge", "Confined check runs by state.",
            [({"state": state}, count) for state, count in checks] or [({"state": "CLEANED"}, 0)])
    _metric(lines, "sentinel_eval_runs_total", "counter", "Recorded eval runs.", [({}, eval_total)])
    samples_rate, samples_low, samples_high = [], [], []
    for run_id, config_json, summary_json in evals:
        config, summary = json.loads(config_json), json.loads(summary_json)
        labels = {"run_id": run_id, "config": config.get("name"), "agent": config.get("agent")}
        samples_rate.append((labels, summary["rate"]))
        samples_low.append((labels, summary["low"]))
        samples_high.append((labels, summary["high"]))
    _metric(lines, "sentinel_eval_pass_rate", "gauge",
            f"Pass rate of the latest {eval_runs} recorded eval runs.", samples_rate)
    _metric(lines, "sentinel_eval_pass_rate_low", "gauge", "Wilson 95% lower bound.", samples_low)
    _metric(lines, "sentinel_eval_pass_rate_high", "gauge", "Wilson 95% upper bound.", samples_high)
    return "\n".join(lines) + "\n"
