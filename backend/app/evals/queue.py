"""A persistent eval job queue in SQLite (WAL) with leases, fencing and crash recovery.

One row per (run, task, attempt). Workers claim a job atomically (``BEGIN
IMMEDIATE``): the oldest QUEUED job, or a RUNNING one whose lease expired (its
worker died). Each claim gets a fresh ``claim_token``; heartbeats extend the
lease, and :meth:`JobQueue.complete` succeeds only while the caller still holds
the current token. A worker that lost its lease (stalled, then reclaimed by
another) cannot record a result, so every job is *completed* exactly once even
though, after a crash, it may *execute* more than once. A job whose claims reach
``max_claims`` without completing becomes FAILED instead of looping forever.

No broker, no extra dependency: SQLite in WAL mode serializes the claim and the
queue file survives the process, so ``recover`` after a SIGKILL simply makes the
expired leases claimable again.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

QUEUED, RUNNING, DONE, FAILED = "QUEUED", "RUNNING", "DONE", "FAILED"
DEFAULT_LEASE_SECONDS = 60.0
DEFAULT_MAX_CLAIMS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    state TEXT NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED')),
    lease_owner TEXT NULL,
    lease_expires_at REAL NULL,
    claim_token TEXT NULL,
    claims INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NULL,
    error TEXT NULL,
    enqueued_at REAL NOT NULL,
    first_claimed_at REAL NULL,
    completed_at REAL NULL,
    UNIQUE (run_id, task_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_eval_jobs_claim ON eval_jobs(state, enqueued_at);
"""


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    run_id: str
    task_id: str
    attempt: int
    claim_token: str
    lease_owner: str
    claims: int


class JobQueue:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time,
                 max_claims: int = DEFAULT_MAX_CLAIMS) -> None:
        self.path = Path(path)
        self._clock = clock
        self._max_claims = max_claims
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def enqueue(self, run_id: str, task_ids: Iterable[str], k: int) -> int:
        """Add k attempts per task (existing rows are kept, so re-enqueueing is idempotent)."""
        now = self._clock()
        added = 0
        with self._transaction() as connection:
            for task_id in task_ids:
                for attempt in range(1, k + 1):
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO eval_jobs (id, run_id, task_id, attempt, state, "
                        "enqueued_at) VALUES (?, ?, ?, ?, 'QUEUED', ?)",
                        (str(uuid4()), run_id, task_id, attempt, now))
                    added += cursor.rowcount
        return added

    def claim(self, owner: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> Job | None:
        now = self._clock()
        token = uuid4().hex
        with self._transaction() as connection:
            # Jobs whose lease expired after max_claims claims are given up on.
            connection.execute(
                "UPDATE eval_jobs SET state='FAILED', error='claimed too many times without "
                "completing', lease_owner=NULL, lease_expires_at=NULL, claim_token=NULL "
                "WHERE state='RUNNING' AND lease_expires_at < ? AND claims >= ?",
                (now, self._max_claims))
            row = connection.execute(
                "SELECT id FROM eval_jobs WHERE state='QUEUED' "
                "OR (state='RUNNING' AND lease_expires_at < ?) "
                "ORDER BY enqueued_at, run_id, task_id, attempt LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE eval_jobs SET state='RUNNING', lease_owner=?, lease_expires_at=?, "
                "claim_token=?, claims=claims+1, first_claimed_at=COALESCE(first_claimed_at, ?) "
                "WHERE id=?", (owner, now + lease_seconds, token, now, row[0]))
            job = connection.execute(
                "SELECT id, run_id, task_id, attempt, claim_token, lease_owner, claims "
                "FROM eval_jobs WHERE id=?", (row[0],)).fetchone()
        return Job(*job)

    def heartbeat(self, job: Job, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> bool:
        """Extend the lease; False when the job was reclaimed (stop working on it)."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE eval_jobs SET lease_expires_at=? WHERE id=? AND claim_token=? "
                "AND state='RUNNING'", (self._clock() + lease_seconds, job.id, job.claim_token))
        return cursor.rowcount == 1

    def complete(self, job: Job, result: dict[str, Any]) -> bool:
        """Record the result if this claim is still current; exactly one completion wins."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE eval_jobs SET state='DONE', result_json=?, completed_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL WHERE id=? AND claim_token=? "
                "AND state='RUNNING'",
                (json.dumps(result, sort_keys=True), self._clock(), job.id, job.claim_token))
        return cursor.rowcount == 1

    def release(self, job: Job, error: str) -> None:
        """Give a job back after a handler error (FAILED once it used its claims)."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE eval_jobs SET state=CASE WHEN claims >= ? THEN 'FAILED' ELSE 'QUEUED' END, "
                "error=?, lease_owner=NULL, lease_expires_at=NULL, claim_token=NULL "
                "WHERE id=? AND claim_token=? AND state='RUNNING'",
                (self._max_claims, error[:1000], job.id, job.claim_token))

    def recover(self) -> int:
        """Startup: make every RUNNING job claimable now (its owner died with the process)."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE eval_jobs SET state='QUEUED', lease_owner=NULL, lease_expires_at=NULL, "
                "claim_token=NULL WHERE state='RUNNING'")
        return cursor.rowcount

    def counts(self, run_id: str | None = None) -> dict[str, int]:
        query = "SELECT state, COUNT(*) FROM eval_jobs"
        args: tuple = ()
        if run_id is not None:
            query += " WHERE run_id=?"
            args = (run_id,)
        with self._connect() as connection:
            rows = connection.execute(query + " GROUP BY state", args).fetchall()
        counts = {QUEUED: 0, RUNNING: 0, DONE: 0, FAILED: 0}
        counts.update(dict(rows))
        return counts

    def results(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT result_json FROM eval_jobs WHERE run_id=? AND state='DONE' "
                "ORDER BY task_id, attempt", (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def timings(self, run_id: str) -> list[tuple[float, float | None, float | None]]:
        """(enqueued, first claimed, completed) per job, for queue-delay measurements."""
        with self._connect() as connection:
            return connection.execute(
                "SELECT enqueued_at, first_claimed_at, completed_at FROM eval_jobs WHERE run_id=?",
                (run_id,)).fetchall()
