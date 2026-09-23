"""Transactional task/dependency persistence with optimistic concurrency,
idempotency, and atomic whole-graph cycle rejection.

Follows `backend/app/core/change_repository.py::ChangeRepository` conventions
throughout: one `BEGIN IMMEDIATE` connection per public method, idempotency
replay checked first inside that same transaction, a paired `JournalWriter`
event on the same connection as the domain write, and a JSON idempotency
payload independent of the live row shape.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from backend.app.contracts.models import JournalEventType, TaskState
from backend.app.coordination.errors import (
    task_dependency_cross_change,
    task_dependency_cycle,
    task_dependency_not_found,
    task_dependency_self,
    task_invalid_transition,
    task_not_editable,
    task_revision_conflict,
)
from backend.app.coordination.models import StoredTask
from backend.app.core.database import Database
from backend.app.core.errors import idempotency_conflict
from backend.app.core.journal import JournalWriter


_EDITABLE_STATES = frozenset({TaskState.DRAFT})
_SUBMITTABLE_STATES = frozenset({TaskState.DRAFT})
_CANCELLABLE_STATES = frozenset({TaskState.DRAFT, TaskState.WAITING, TaskState.READY})


class TaskRepository:
    def __init__(self, database: Database, *, journal: JournalWriter | None = None) -> None:
        self.database = database
        self.journal = journal

    # -- create / read ----------------------------------------------------

    def create(
        self,
        task: StoredTask,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> StoredTask:
        scope = "coord_tasks:create"
        with self.database.connection(immediate=True) as connection:
            replay = self._replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            next_seq_row = connection.execute(
                "SELECT COALESCE(MAX(enqueue_seq), 0) + 1 AS next_seq "
                "FROM coord_tasks WHERE change_id = ?",
                (str(task.change_id),),
            ).fetchone()
            resolved = replace(task, enqueue_seq=int(next_seq_row["next_seq"]))
            connection.execute(
                """
                INSERT INTO coord_tasks (
                    id, change_id, title, instructions, creator_actor_id,
                    assigned_actor_id, adapter, state, revision, priority,
                    enqueue_seq, max_attempts, execution_timeout_seconds,
                    waiting_reason, failure_reason_json, created_at, updated_at,
                    submitted_at, executable, args_json, write_paths_json,
                    verification_json, resources_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?)
                """,
                self._task_values(resolved),
            )
            if self.journal is not None:
                self.journal.append(
                    resolved.change_id, JournalEventType.TASK_CREATED,
                    subject_type="task", subject_id=resolved.id,
                    payload={"title": resolved.title, "adapter": resolved.adapter},
                    connection=connection,
                )
            self._record_result(connection, scope, idempotency_key, request_hash, resolved)
            return resolved

    def get(self, task_id: UUID) -> StoredTask | None:
        with self.database.connection() as connection:
            row = self._select_task(connection, task_id)
            if row is None:
                return None
            return self._from_row(connection, row)

    def list_for_change(
        self, change_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[StoredTask]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM coord_tasks WHERE change_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (str(change_id), limit, offset),
            ).fetchall()
            return [self._from_row(connection, row) for row in rows]

    def count_for_change(self, change_id: UUID) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM coord_tasks WHERE change_id = ?",
                (str(change_id),),
            ).fetchone()
            return int(row["n"])

    # -- mutations ----------------------------------------------------------

    def edit(
        self,
        task_id: UUID,
        expected_revision: int,
        updated_at: datetime,
        fields: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> StoredTask | None:
        scope = f"coord_task:{task_id}:edit"
        with self.database.connection(immediate=True) as connection:
            replay = self._replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            row = self._select_task(connection, task_id)
            if row is None:
                return None
            current = self._from_row(connection, row)
            self._check_revision(current, expected_revision)
            if current.state not in _EDITABLE_STATES:
                raise task_not_editable(str(task_id), current.state.value)
            columns: list[str] = []
            values: list[Any] = []
            for column, value in fields.items():
                if value is None:
                    continue
                columns.append(f"{column} = ?")
                values.append(value)
            if columns:
                connection.execute(
                    f"UPDATE coord_tasks SET {', '.join(columns)}, "
                    "revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?",
                    (*values, updated_at.isoformat(), str(task_id), expected_revision),
                )
            updated = self._require_task(connection, task_id)
            # An edit that names no fields changes nothing, so it records nothing.
            if self.journal is not None and columns:
                self.journal.append(
                    updated.change_id, JournalEventType.TASK_EDITED,
                    subject_type="task", subject_id=task_id,
                    payload={"fields": sorted(fields.keys())}, connection=connection,
                )
            self._record_result(connection, scope, idempotency_key, request_hash, updated)
            return updated

    def replace_dependencies(
        self,
        task_id: UUID,
        change_id: UUID,
        depends_on_task_ids: list[UUID],
        expected_revision: int,
        updated_at: datetime,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> StoredTask | None:
        scope = f"coord_task:{task_id}:dependencies"
        with self.database.connection(immediate=True) as connection:
            replay = self._replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            row = self._select_task(connection, task_id)
            if row is None:
                return None
            current = self._from_row(connection, row)
            self._check_revision(current, expected_revision)
            if current.state not in _EDITABLE_STATES:
                raise task_not_editable(str(task_id), current.state.value)

            for predecessor_id in depends_on_task_ids:
                if predecessor_id == task_id:
                    raise task_dependency_self(str(task_id))
                predecessor_row = self._select_task(connection, predecessor_id)
                if predecessor_row is None:
                    raise task_dependency_not_found(str(predecessor_id))
                if UUID(predecessor_row["change_id"]) != change_id:
                    raise task_dependency_cross_change(
                        str(task_id), str(predecessor_id)
                    )

            connection.execute(
                "DELETE FROM coord_dependencies WHERE task_id = ?", (str(task_id),)
            )
            for predecessor_id in depends_on_task_ids:
                connection.execute(
                    """
                    INSERT INTO coord_dependencies (
                        id, change_id, task_id, depends_on_task_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()), str(change_id), str(task_id),
                        str(predecessor_id), updated_at.isoformat(),
                    ),
                )

            cycle = self._find_cycle(connection, change_id)
            if cycle is not None:
                raise task_dependency_cycle(cycle)

            connection.execute(
                """
                UPDATE coord_tasks SET revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (updated_at.isoformat(), str(task_id), expected_revision),
            )
            updated = self._require_task(connection, task_id)
            if self.journal is not None:
                self.journal.append(
                    change_id, JournalEventType.TASK_DEPENDENCIES_REPLACED,
                    subject_type="task", subject_id=task_id,
                    payload={
                        "depends_on_task_ids": [str(i) for i in depends_on_task_ids]
                    },
                    connection=connection,
                )
            self._record_result(connection, scope, idempotency_key, request_hash, updated)
            return updated

    def submit(
        self,
        task_id: UUID,
        expected_revision: int,
        updated_at: datetime,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> StoredTask | None:
        scope = f"coord_task:{task_id}:submit"
        with self.database.connection(immediate=True) as connection:
            replay = self._replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            row = self._select_task(connection, task_id)
            if row is None:
                return None
            current = self._from_row(connection, row)
            self._check_revision(current, expected_revision)
            if current.state not in _SUBMITTABLE_STATES:
                raise task_invalid_transition(
                    str(task_id), current.state.value, "submitted"
                )

            predecessor_rows = connection.execute(
                """
                SELECT t.state AS state FROM coord_dependencies d
                JOIN coord_tasks t ON t.id = d.depends_on_task_id
                WHERE d.task_id = ?
                """,
                (str(task_id),),
            ).fetchall()
            unmet = sum(
                1 for r in predecessor_rows if r["state"] != TaskState.SUCCEEDED.value
            )
            if unmet > 0:
                new_state = TaskState.WAITING
                waiting_reason = (
                    f"waiting on {unmet} predecessor task(s) to reach SUCCEEDED"
                )
            else:
                new_state = TaskState.READY
                waiting_reason = None

            connection.execute(
                """
                UPDATE coord_tasks
                SET state = ?, waiting_reason = ?, revision = revision + 1,
                    updated_at = ?, submitted_at = ?
                WHERE id = ? AND revision = ?
                """,
                (
                    new_state.value, waiting_reason, updated_at.isoformat(),
                    updated_at.isoformat(), str(task_id), expected_revision,
                ),
            )
            updated = self._require_task(connection, task_id)
            if self.journal is not None:
                self.journal.append(
                    updated.change_id, JournalEventType.TASK_SUBMITTED,
                    subject_type="task", subject_id=task_id,
                    payload={"state": new_state.value}, connection=connection,
                )
            self._record_result(connection, scope, idempotency_key, request_hash, updated)
            return updated

    def cancel(
        self,
        task_id: UUID,
        expected_revision: int,
        updated_at: datetime,
        reason: str | None,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> StoredTask | None:
        scope = f"coord_task:{task_id}:cancel"
        with self.database.connection(immediate=True) as connection:
            replay = self._replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            row = self._select_task(connection, task_id)
            if row is None:
                return None
            current = self._from_row(connection, row)
            self._check_revision(current, expected_revision)
            if current.state not in _CANCELLABLE_STATES:
                raise task_invalid_transition(
                    str(task_id), current.state.value, "cancelled"
                )
            connection.execute(
                """
                UPDATE coord_tasks
                SET state = ?, waiting_reason = NULL, failure_reason_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (
                    TaskState.CANCELLED.value,
                    json.dumps(reason) if reason is not None else None,
                    updated_at.isoformat(), str(task_id), expected_revision,
                ),
            )
            updated = self._require_task(connection, task_id)
            if self.journal is not None:
                self.journal.append(
                    updated.change_id, JournalEventType.TASK_CANCELLED,
                    subject_type="task", subject_id=task_id,
                    payload={"reason": reason}, connection=connection,
                )
            self._record_result(connection, scope, idempotency_key, request_hash, updated)
            return updated

    # -- cycle detection ------------------------------------------------------

    @staticmethod
    def _find_cycle(connection: sqlite3.Connection, change_id: UUID) -> list[str] | None:
        """Whole-graph DFS cycle check for one Change; see build-status decision log."""

        rows = connection.execute(
            "SELECT task_id, depends_on_task_id FROM coord_dependencies WHERE change_id = ?",
            (str(change_id),),
        ).fetchall()
        edges: dict[str, list[str]] = {}
        for r in rows:
            edges.setdefault(r["task_id"], []).append(r["depends_on_task_id"])

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {}
        path: list[str] = []

        def visit(node: str) -> list[str] | None:
            color[node] = GRAY
            path.append(node)
            for nxt in edges.get(node, ()):
                state = color.get(nxt, WHITE)
                if state == WHITE:
                    found = visit(nxt)
                    if found is not None:
                        return found
                elif state == GRAY:
                    idx = path.index(nxt)
                    return path[idx:] + [nxt]
            color[node] = BLACK
            path.pop()
            return None

        for node in list(edges.keys()):
            if color.get(node, WHITE) == WHITE:
                found = visit(node)
                if found is not None:
                    return found
        return None

    # -- internals --------------------------------------------------------

    @staticmethod
    def _select_task(connection: sqlite3.Connection, task_id: UUID) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM coord_tasks WHERE id = ?", (str(task_id),)
        ).fetchone()

    def _require_task(self, connection: sqlite3.Connection, task_id: UUID) -> StoredTask:
        row = self._select_task(connection, task_id)
        if row is None:
            raise RuntimeError("Task disappeared during a transaction")
        return self._from_row(connection, row)

    @staticmethod
    def _check_revision(task: StoredTask, expected: int) -> None:
        if task.revision != expected:
            raise task_revision_conflict(expected=expected, actual=task.revision)

    def _from_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> StoredTask:
        edge_rows = connection.execute(
            "SELECT depends_on_task_id FROM coord_dependencies "
            "WHERE task_id = ? ORDER BY created_at",
            (row["id"],),
        ).fetchall()
        failure_payload = row["failure_reason_json"]
        return StoredTask(
            id=UUID(row["id"]),
            change_id=UUID(row["change_id"]),
            title=row["title"],
            instructions=row["instructions"],
            adapter=row["adapter"],
            creator_actor_id=(
                UUID(row["creator_actor_id"]) if row["creator_actor_id"] else None
            ),
            assigned_actor_id=(
                UUID(row["assigned_actor_id"]) if row["assigned_actor_id"] else None
            ),
            state=TaskState(row["state"]),
            revision=int(row["revision"]),
            priority=int(row["priority"]),
            enqueue_seq=int(row["enqueue_seq"]),
            max_attempts=int(row["max_attempts"]),
            execution_timeout_seconds=int(row["execution_timeout_seconds"]),
            waiting_reason=row["waiting_reason"],
            failure_reason=(json.loads(failure_payload) if failure_payload else None),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            submitted_at=(
                datetime.fromisoformat(row["submitted_at"])
                if row["submitted_at"]
                else None
            ),
            depends_on_task_ids=tuple(
                UUID(r["depends_on_task_id"]) for r in edge_rows
            ),
            executable=row["executable"],
            args=tuple(json.loads(row["args_json"])),
            write_paths=tuple(json.loads(row["write_paths_json"])),
            verification=tuple(json.loads(row["verification_json"])),
            resources=tuple(json.loads(row["resources_json"])),
        )

    @staticmethod
    def _task_values(task: StoredTask) -> tuple[Any, ...]:
        return (
            str(task.id),
            str(task.change_id),
            task.title,
            task.instructions,
            str(task.creator_actor_id) if task.creator_actor_id else None,
            str(task.assigned_actor_id) if task.assigned_actor_id else None,
            task.adapter,
            task.state.value,
            task.revision,
            task.priority,
            task.enqueue_seq,
            task.max_attempts,
            task.execution_timeout_seconds,
            task.waiting_reason,
            json.dumps(task.failure_reason) if task.failure_reason is not None else None,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
            task.submitted_at.isoformat() if task.submitted_at else None,
            task.executable,
            json.dumps(list(task.args)),
            json.dumps(list(task.write_paths)),
            json.dumps(list(task.verification)),
            json.dumps(list(task.resources)),
        )

    # -- idempotency envelope (mirrors ChangeRepository) -----------------

    def _replay(
        self,
        connection: sqlite3.Connection,
        scope: str,
        key: str | None,
        request_hash: str | None,
    ) -> StoredTask | None:
        if key is None:
            return None
        if request_hash is None:
            raise ValueError("request_hash is required with an idempotency key")
        row = connection.execute(
            "SELECT request_hash, result_json FROM idempotency_records "
            "WHERE scope = ? AND key = ?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise idempotency_conflict(scope)
        payload = json.loads(row["result_json"])
        if payload is None:
            return None
        return self._payload_to_task(payload)

    def _record_result(
        self,
        connection: sqlite3.Connection,
        scope: str,
        key: str | None,
        request_hash: str | None,
        task: StoredTask | None,
    ) -> None:
        if key is None:
            return
        if request_hash is None:
            raise ValueError("request_hash is required with an idempotency key")
        payload = self._task_to_payload(task) if task is not None else None
        connection.execute(
            """
            INSERT INTO idempotency_records (
                scope, key, request_hash, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                scope, key, request_hash,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _task_to_payload(task: StoredTask) -> dict[str, Any]:
        return {
            "id": str(task.id),
            "change_id": str(task.change_id),
            "title": task.title,
            "instructions": task.instructions,
            "adapter": task.adapter,
            "creator_actor_id": (
                str(task.creator_actor_id) if task.creator_actor_id else None
            ),
            "assigned_actor_id": (
                str(task.assigned_actor_id) if task.assigned_actor_id else None
            ),
            "state": task.state.value,
            "revision": task.revision,
            "priority": task.priority,
            "enqueue_seq": task.enqueue_seq,
            "max_attempts": task.max_attempts,
            "execution_timeout_seconds": task.execution_timeout_seconds,
            "waiting_reason": task.waiting_reason,
            "failure_reason": task.failure_reason,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "submitted_at": task.submitted_at.isoformat() if task.submitted_at else None,
            "depends_on_task_ids": [str(i) for i in task.depends_on_task_ids],
            "executable": task.executable,
            "args": list(task.args),
            "write_paths": list(task.write_paths),
            "verification": list(task.verification),
            "resources": list(task.resources),
        }

    @staticmethod
    def _payload_to_task(payload: dict[str, Any]) -> StoredTask:
        return StoredTask(
            id=UUID(payload["id"]),
            change_id=UUID(payload["change_id"]),
            title=payload["title"],
            instructions=payload["instructions"],
            adapter=payload["adapter"],
            creator_actor_id=(
                UUID(payload["creator_actor_id"])
                if payload.get("creator_actor_id")
                else None
            ),
            assigned_actor_id=(
                UUID(payload["assigned_actor_id"])
                if payload.get("assigned_actor_id")
                else None
            ),
            state=TaskState(payload["state"]),
            revision=int(payload["revision"]),
            priority=int(payload["priority"]),
            enqueue_seq=int(payload["enqueue_seq"]),
            max_attempts=int(payload["max_attempts"]),
            execution_timeout_seconds=int(payload["execution_timeout_seconds"]),
            waiting_reason=payload.get("waiting_reason"),
            failure_reason=payload.get("failure_reason"),
            created_at=datetime.fromisoformat(payload["created_at"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            submitted_at=(
                datetime.fromisoformat(payload["submitted_at"])
                if payload.get("submitted_at")
                else None
            ),
            depends_on_task_ids=tuple(
                UUID(i) for i in payload.get("depends_on_task_ids", ())
            ),
            # Records written before these fields existed replay as empty.
            executable=payload.get("executable"),
            args=tuple(payload.get("args", ())),
            write_paths=tuple(payload.get("write_paths", ())),
            verification=tuple(payload.get("verification", ())),
            resources=tuple(payload.get("resources", ())),
        )
