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
from backend.app.evals.report import SCHEMA, summarize

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
