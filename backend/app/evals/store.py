"""Persisted eval runs (migration 014) with journal events on each attempt's Change.

A run and all its attempts are written in one transaction. An attempt that ran
on a real Change (the API driver records ``agent.change_id``) also appends
``eval.result_recorded`` to that Change's hash-chained journal in the same
transaction. Mock attempts have no Change and no journal event; that is stated,
not hidden: their rows say ``change_id = NULL``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from backend.app.contracts.models import JournalEventType
from backend.app.core.database import Database
from backend.app.core.errors import AppError
from backend.app.core.journal import JournalWriter
from backend.app.evals.report import SCHEMA, compare, summarize

MAX_RESULTS = 20_000


def eval_run_not_found(run_id: str) -> AppError:
    return AppError("EVAL_RUN_NOT_FOUND", "The eval run does not exist.", status_code=404,
                    details={"run_id": run_id})


class EvalStore:
    def __init__(self, database: Database, *, journal: JournalWriter | None = None) -> None:
        self._database = database
        self._journal = journal

    def record(self, document: dict[str, Any]) -> dict[str, Any]:
        if document.get("schema") != SCHEMA or not isinstance(document.get("results"), list):
            raise AppError("EVAL_RUN_INVALID", f"An eval run must be a {SCHEMA} document.")
        if len(document["results"]) > MAX_RESULTS:
            raise AppError("EVAL_RUN_INVALID", "The eval run has too many results.")
        run_id = str(UUID(str(document["id"])))
        try:
            summary = summarize(document)
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError("EVAL_RUN_INVALID", "The eval run results are malformed.") from exc
        now = datetime.now(UTC).isoformat()
        with self._database.connection() as connection:
            if connection.execute("SELECT 1 FROM eval_runs WHERE id = ?", (run_id,)).fetchone():
                raise AppError("EVAL_RUN_EXISTS", "This eval run was already recorded.",
                               status_code=409, details={"run_id": run_id})
            connection.execute(
                "INSERT INTO eval_runs (id, suite, config_json, k, started_at, completed_at, "
                "summary_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, str(document.get("suite")), json.dumps(document["config"], sort_keys=True),
                 int(document["k"]), str(document.get("started_at")),
                 document.get("completed_at"), json.dumps(summary.to_payload(), sort_keys=True), now))
            for result in document["results"]:
                change_id = (result.get("agent") or {}).get("change_id")
                known = change_id is not None and connection.execute(
                    "SELECT 1 FROM changes WHERE id = ?", (str(change_id),)).fetchone() is not None
                connection.execute(
                    "INSERT INTO eval_results (run_id, task_id, attempt, status, change_id, "
                    "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, str(result["task_id"]), int(result["attempt"]), str(result["status"]),
                     str(change_id) if known else None, json.dumps(result, sort_keys=True), now))
                if known and self._journal is not None:
                    self._journal.append(
                        UUID(str(change_id)), JournalEventType.EVAL_RESULT_RECORDED,
                        subject_type="eval_run", subject_id=UUID(run_id),
                        payload={"task_id": result["task_id"], "attempt": result["attempt"],
                                 "status": result["status"],
                                 "hidden_tests_absent": result.get("hidden_tests_absent"),
                                 "hidden_boundary": (result.get("hidden") or {}).get("boundary")},
                        connection=connection)
        return self.get(run_id)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT id, suite, config_json, k, started_at, completed_at, summary_json "
                "FROM eval_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._view(row) for row in rows]

    def get(self, run_id: str) -> dict[str, Any]:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT id, suite, config_json, k, started_at, completed_at, summary_json "
                "FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise eval_run_not_found(run_id)
        return self._view(row)

    def document(self, run_id: str) -> dict[str, Any]:
        """The run rebuilt as a ``sentinel-eval-run/1`` document from its stored attempts."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT id, suite, config_json, k, started_at, completed_at FROM eval_runs "
                "WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise eval_run_not_found(run_id)
            results = [json.loads(item[0]) for item in connection.execute(
                "SELECT payload_json FROM eval_results WHERE run_id = ? "
                "ORDER BY task_id, attempt", (run_id,)).fetchall()]
        return {"schema": SCHEMA, "id": row[0], "suite": row[1], "config": json.loads(row[2]),
                "k": row[3], "started_at": row[4], "completed_at": row[5], "results": results}

    def attempts(self, run_id: str) -> list[dict[str, Any]]:
        """Every attempt of a run, flattened for display (UNKNOWN stays None)."""
        return [_attempt_view(result) for result in self.document(run_id)["results"]]

    def compare(self, baseline_id: str, candidate_id: str) -> dict[str, Any]:
        """``sentinel eval compare`` over two recorded runs: deltas, bootstrap, verdict."""
        comparison = compare(self.document(baseline_id), self.document(candidate_id))
        rates_a = {task.task_id: task.rate for task in comparison.a.tasks}
        rates_b = {task.task_id: task.rate for task in comparison.b.tasks}
        boot = comparison.bootstrap
        return {
            "baseline_id": baseline_id, "candidate_id": candidate_id,
            "overall_delta": comparison.b.rate - comparison.a.rate,
            "regression": comparison.regression,
            "intervals_separate": (comparison.b.high < comparison.a.low
                                   or comparison.b.low > comparison.a.high),
            "bootstrap": None if boot is None else {
                "mean_difference": boot.mean_difference, "low": boot.interval.low,
                "high": boot.interval.high, "resamples": boot.resamples, "tasks": boot.tasks},
            "tasks": [{"task_id": task, "rate_a": rates_a[task], "rate_b": rates_b[task],
                       "delta": delta,
                       "flip": ("regressed" if task in comparison.regressed else
                                "improved" if task in comparison.improved else None)}
                      for task, delta in sorted(comparison.task_deltas.items())],
            "only_in_a": comparison.only_in_a, "only_in_b": comparison.only_in_b,
        }

    @staticmethod
    def _view(row) -> dict[str, Any]:
        summary = json.loads(row[6])
        config = json.loads(row[2])
        return {
            "id": row[0], "suite": row[1], "config_name": str(config.get("name")),
            "agent": str(config.get("agent")), "model": config.get("model"), "k": row[3],
            "started_at": row[4], "completed_at": row[5],
            "passes": summary["passes"], "attempts": summary["attempts"],
            "errors": summary["errors"], "rate": summary["rate"], "low": summary["low"],
            "high": summary["high"], "mean_cost_usd": summary["mean_cost_usd"],
            "cost_unknown": summary["cost_unknown"],
            "wall_p50_seconds": summary["wall_p50_seconds"],
            "wall_p95_seconds": summary["wall_p95_seconds"],
            "hidden_boundaries": summary["hidden_boundaries"],
            "tasks": [{"task_id": task["task_id"], "passes": task["passes"],
                       "attempts": task["attempts"], "errors": task["errors"],
                       "rate": task["rate"], "low": task["low"], "high": task["high"]}
                      for task in summary["tasks"]],
        }


def _attempt_view(result: dict[str, Any]) -> dict[str, Any]:
    agent = result.get("agent") or {}
    hidden = result.get("hidden") or {}
    return {
        "task_id": str(result["task_id"]), "attempt": int(result["attempt"]),
        "status": str(result["status"]),
        "hidden_tests_absent": result.get("hidden_tests_absent"),
        "wall_seconds": result.get("wall_seconds"), "error": result.get("error"),
        "agent_status": agent.get("status"), "cost_usd": agent.get("cost_usd"),
        "input_tokens": agent.get("input_tokens"), "output_tokens": agent.get("output_tokens"),
        "change_id": agent.get("change_id"), "passport_id": agent.get("passport_id"),
        "hidden_boundary": hidden.get("boundary"), "hidden_exit_code": hidden.get("exit_code"),
        "hidden_timed_out": hidden.get("timed_out"),
        "hidden_output_tail": hidden.get("output_tail"),
    }
