"""Durable workspace records and retention states (plan sections 5 and 9).

`WorkspaceManager` performs the Git side effects; this registry makes each of
them recoverable. Git and SQLite cannot share a transaction, so every external
step is bracketed the same way (plan section 5's atomic transition pattern):

1. A short `BEGIN IMMEDIATE` transaction records the intent (`CREATING`,
   `REMOVING`) together with its journal event.
2. The Git command runs with no database transaction open.
3. A second transaction records the outcome, conditioned on the state the
   first one wrote, so a concurrent or replayed call cannot double-apply it.

A crash between steps leaves a row that says what may exist on disk.
`reconcile()` settles those rows from observed Git state on startup and only
reports, never deletes, directories it has no record of.

Retention (plan section 9 step 6): only `CAPTURED` workspaces, whose result
commit is pinned by a managed ref, and `FAILED` ones may be removed. A
`READY` workspace may hold agent work that was never captured and is never
removed implicitly. Removal deletes a directory only when Git itself lists it
as a worktree of the recorded repository, so a stray or foreign directory
that happens to sit at a recorded path is left alone.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from backend.app.contracts.models import JournalEventType, WorkspacePurpose, WorkspaceState
from backend.app.coordination.errors import workspace_invalid_state, workspace_not_found
from backend.app.coordination.models import StoredWorkspace
from backend.app.coordination.workspaces import (
    MAX_CAPTURE_FILES,
    WorkspaceManager,
    workspace_error,
)
from backend.app.core.database import Database
from backend.app.core.errors import AppError
from backend.app.core.journal import JournalWriter


_REMOVABLE_STATES = frozenset({WorkspaceState.CAPTURED, WorkspaceState.FAILED,
                               WorkspaceState.REMOVING})
# Capture failures that mean the workspace itself can no longer be trusted.
# Others (diverged base, oversized result) leave it READY for inspection.
_FATAL_CAPTURE_CODES = frozenset({"WORKSPACE_CORRUPT"})


@dataclass(slots=True)
class WorkspaceReconcileReport:
    """What `reconcile()` changed and what still needs an operator."""

    readied: list[UUID] = field(default_factory=list)
    failed: list[UUID] = field(default_factory=list)
    removed: list[UUID] = field(default_factory=list)
    pending_removal: list[UUID] = field(default_factory=list)
    orphan_paths: list[str] = field(default_factory=list)


class WorkspaceRegistry:
    def __init__(
        self,
        database: Database,
        manager: WorkspaceManager,
        *,
        journal: JournalWriter | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.manager = manager
        self.journal = journal
        self._clock = clock
        self._locks_guard = threading.Lock()
        # Git worktree administration (add/remove/prune) edits shared
        # metadata under the common Git directory; serialize it per repository.
        self._repository_locks: dict[str, threading.Lock] = {}
        # Capture and removal of one workspace must not interleave.
        self._workspace_locks: dict[UUID, threading.Lock] = {}

    # -- lifecycle ----------------------------------------------------------

    def provision(
        self,
        change_id: UUID,
        repository_path: str,
        *,
        workspace_id: UUID,
        purpose: WorkspacePurpose,
        base_revision: str,
        attempt_id: UUID | None = None,
        branch: str | None = None,
    ) -> StoredWorkspace:
        """Create a worktree pinned to the commit `base_revision` names.

        Attempt workspaces get their own branch (`sentinel/attempt/<id>` by
        default); integration workspaces are detached unless a branch is given.
        """

        identity = self.manager.repository_identity(repository_path)
        base_sha = self.manager.resolve_commit(identity.root, base_revision)
        if branch is None and purpose is WorkspacePurpose.ATTEMPT:
            branch = f"sentinel/attempt/{workspace_id}"
        now = self._clock()
        record = StoredWorkspace(
            id=workspace_id, change_id=change_id, purpose=purpose,
            repository_identity=identity.identity, repository_root=identity.root,
            path=str(self.manager.managed_root / str(workspace_id)), base_sha=base_sha,
            state=WorkspaceState.CREATING, created_at=now, updated_at=now,
            attempt_id=attempt_id, branch=branch,
        )
        with self.database.connection(immediate=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO coord_workspaces (
                        id, attempt_id, change_id, purpose, repository_identity,
                        repository_root, path, branch, base_sha, state, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(record.id), _opt(record.attempt_id), str(change_id), purpose.value,
                     record.repository_identity, record.repository_root, record.path,
                     branch, base_sha, record.state.value, now.isoformat(), now.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise workspace_error("WORKSPACE_EXISTS", "The workspace is already recorded.",
                                      workspace_id=str(workspace_id)) from exc
            self._journal(connection, record, JournalEventType.WORKSPACE_CREATING, {
                "purpose": purpose.value, "base_sha": base_sha, "branch": branch,
                "attempt_id": _opt(attempt_id),
            })
        try:
            with self._repository_lock(identity.identity):
                self.manager.create(identity.root, str(workspace_id), base_sha, branch)
        except Exception as exc:
            detail = exc.code if isinstance(exc, AppError) else type(exc).__name__
            self._transition(record, {WorkspaceState.CREATING}, WorkspaceState.FAILED,
                             JournalEventType.WORKSPACE_FAILED, detail=f"create: {detail}")
            raise
        return self._transition(record, {WorkspaceState.CREATING}, WorkspaceState.READY,
                                JournalEventType.WORKSPACE_READY)

    def capture(
        self,
        workspace_id: UUID,
        *,
        write_paths: list[str],
        message: str,
        max_files: int = MAX_CAPTURE_FILES,
    ) -> StoredWorkspace:
        """Commit the workspace's edits and pin the result.

        Idempotent: a workspace that is already `CAPTURED` returns its stored
        record without running Git again, so a replayed finalization cannot
        mint a second result commit. A capture interrupted after its commit
        is completed from the committed state on retry.
        """

        with self._workspace_lock(workspace_id):
            record = self._require(workspace_id)
            if record.state is WorkspaceState.CAPTURED:
                return record
            if record.state is not WorkspaceState.READY:
                raise workspace_invalid_state(str(workspace_id), record.state.value, "captured")
            try:
                result = self.manager.capture(Path(record.path), base_sha=record.base_sha,
                                              write_paths=write_paths, message=message,
                                              max_files=max_files)
            except AppError as exc:
                if exc.code in _FATAL_CAPTURE_CODES:
                    self._transition(record, {WorkspaceState.READY}, WorkspaceState.FAILED,
                                     JournalEventType.WORKSPACE_FAILED,
                                     detail=f"capture: {exc.code}")
                raise
            result_ref = self.manager.pin_result(record.repository_root, str(workspace_id),
                                                 result.result_sha)
            manifest = result.to_manifest()
            return self._transition(
                record, {WorkspaceState.READY}, WorkspaceState.CAPTURED,
                JournalEventType.WORKSPACE_CAPTURED,
                columns={"result_sha": result.result_sha, "result_ref": result_ref,
                         "capture_json": json.dumps(manifest, sort_keys=True)},
                payload={"result_sha": result.result_sha, "no_change": result.no_change,
                         "file_count": len(result.files),
                         "scope_violations": len(result.scope_violations),
                         "excluded_sensitive": len(result.excluded_sensitive),
                         "sensitive_committed": len(result.sensitive_committed),
                         "conflict_markers": len(result.conflict_markers)},
            )

    def remove(self, workspace_id: UUID, *, attempt_live: bool) -> StoredWorkspace:
        """Delete a retained worktree. The result ref (and so the result
        commit) survives removal. Idempotent for an already-removed record."""

        with self._workspace_lock(workspace_id):
            record = self._require(workspace_id)
            if record.state is WorkspaceState.REMOVED:
                return record
            if attempt_live:
                raise workspace_error("WORKSPACE_ACTIVE",
                                      "An active attempt's workspace cannot be removed.",
                                      workspace_id=str(workspace_id))
            if record.state not in _REMOVABLE_STATES:
                raise workspace_invalid_state(str(workspace_id), record.state.value, "removed")
            if record.state is not WorkspaceState.REMOVING:
                record = self._transition(record, _REMOVABLE_STATES, WorkspaceState.REMOVING,
                                          JournalEventType.WORKSPACE_REMOVING)
            with self._repository_lock(record.repository_identity):
                self._delete_worktree(record)
            now = self._clock()
            return self._transition(record, {WorkspaceState.REMOVING}, WorkspaceState.REMOVED,
                                    JournalEventType.WORKSPACE_REMOVED,
                                    columns={"removed_at": now.isoformat()})

    # -- reads --------------------------------------------------------------

    def get(self, workspace_id: UUID) -> StoredWorkspace | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM coord_workspaces WHERE id = ?",
                                     (str(workspace_id),)).fetchone()
            return _from_row(row) if row is not None else None

    def list_for_change(self, change_id: UUID) -> list[StoredWorkspace]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM coord_workspaces WHERE change_id = ? ORDER BY created_at, id",
                (str(change_id),),
            ).fetchall()
            return [_from_row(row) for row in rows]

    def for_attempt(self, attempt_id: UUID) -> list[StoredWorkspace]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM coord_workspaces WHERE attempt_id = ? ORDER BY created_at, id",
                (str(attempt_id),),
            ).fetchall()
            return [_from_row(row) for row in rows]

    # -- recovery -----------------------------------------------------------

    def reconcile(self) -> WorkspaceReconcileReport:
        """Settle rows left mid-transition by a crash, from observed Git state.

        - `CREATING`: `READY` if Git registers the worktree at its base,
          otherwise `FAILED` (never re-run: the base may have been decided by
          a scheduler that no longer exists).
        - `REMOVING`: `REMOVED` once the directory is gone, otherwise left for
          `remove()` to retry and reported as pending.
        - `READY` with its directory missing: `FAILED` (uncaptured work lost).
        - `CAPTURED` with its directory missing: `REMOVED` (result is pinned).
        - Directories under the managed root with no record: reported only.
        """

        report = WorkspaceReconcileReport()
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM coord_workspaces WHERE state != ? ORDER BY created_at, id",
                (WorkspaceState.REMOVED.value,),
            ).fetchall()
            known_paths = {
                WorkspaceManager._canonical(Path(row["path"]))
                for row in connection.execute("SELECT path FROM coord_workspaces").fetchall()
            }
        registered: dict[str, set[str]] = {}
        for record in (_from_row(row) for row in rows):
            path = Path(record.path)
            exists = path.exists()
            if record.state is WorkspaceState.CREATING:
                worktrees = registered.get(record.repository_root)
                if worktrees is None:
                    worktrees = self._registered(record.repository_root)
                    registered[record.repository_root] = worktrees
                if exists and WorkspaceManager._canonical(path) in worktrees \
                        and self._head_or_none(path) == record.base_sha:
                    self._transition(record, {WorkspaceState.CREATING}, WorkspaceState.READY,
                                     JournalEventType.WORKSPACE_READY,
                                     payload={"reconciled": True})
                    report.readied.append(record.id)
                else:
                    self._transition(record, {WorkspaceState.CREATING}, WorkspaceState.FAILED,
                                     JournalEventType.WORKSPACE_FAILED,
                                     detail="reconcile: creation was interrupted",
                                     payload={"reconciled": True})
                    report.failed.append(record.id)
            elif record.state is WorkspaceState.REMOVING:
                if exists:
                    report.pending_removal.append(record.id)
                else:
                    self._transition(record, {WorkspaceState.REMOVING}, WorkspaceState.REMOVED,
                                     JournalEventType.WORKSPACE_REMOVED,
                                     columns={"removed_at": self._clock().isoformat()},
                                     payload={"reconciled": True})
                    report.removed.append(record.id)
            elif record.state is WorkspaceState.READY and not exists:
                self._transition(record, {WorkspaceState.READY}, WorkspaceState.FAILED,
                                 JournalEventType.WORKSPACE_FAILED,
                                 detail="reconcile: directory missing; uncaptured work lost",
                                 payload={"reconciled": True})
                report.failed.append(record.id)
            elif record.state is WorkspaceState.CAPTURED and not exists:
                self._transition(record, {WorkspaceState.CAPTURED}, WorkspaceState.REMOVED,
                                 JournalEventType.WORKSPACE_REMOVED,
                                 detail="reconcile: removed outside Sentinel; result pinned",
                                 columns={"removed_at": self._clock().isoformat()},
                                 payload={"reconciled": True})
                report.removed.append(record.id)
        for child in sorted(self.manager.managed_root.iterdir()):
            if child.name.startswith("."):
                continue
            if WorkspaceManager._canonical(child) not in known_paths:
                report.orphan_paths.append(str(child))
        return report

    # -- internals ----------------------------------------------------------

    def _delete_worktree(self, record: StoredWorkspace) -> None:
        path = Path(record.path)
        if not path.exists():
            self.manager.prune(record.repository_root)
            return
        if WorkspaceManager._canonical(path) not in self._registered(record.repository_root):
            # The recorded path holds a directory Git does not list as one of
            # this repository's worktrees; it may not be Sentinel's to delete.
            self._set_detail(record, "remove: directory is not a registered worktree")
            raise workspace_error("WORKSPACE_UNOWNED_DIRECTORY",
                                  "The workspace path is not a registered worktree; "
                                  "it was left in place.",
                                  workspace_id=str(record.id), path=record.path)
        self.manager.remove(record.repository_root, path, attempt_live=False)

    def _registered(self, repository_root: str) -> set[str]:
        try:
            return self.manager.registered_worktrees(repository_root)
        except Exception:
            # An unreadable repository registers nothing; callers then treat
            # every directory as unowned, which is the safe direction.
            return set()

    def _head_or_none(self, path: Path) -> str | None:
        try:
            return self.manager.head(path)
        except Exception:
            return None

    def _transition(
        self,
        record: StoredWorkspace,
        from_states: set[WorkspaceState] | frozenset[WorkspaceState],
        to_state: WorkspaceState,
        event: JournalEventType,
        *,
        detail: str | None = None,
        columns: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> StoredWorkspace:
        """Compare-and-set the state; the journal event commits with it."""

        now = self._clock()
        assignments = {"state": to_state.value, "updated_at": now.isoformat(),
                       **(columns or {})}
        if detail is not None:
            assignments["detail"] = detail
        placeholders = ", ".join(f"{name} = ?" for name in assignments)
        allowed = sorted(state.value for state in from_states)
        with self.database.connection(immediate=True) as connection:
            cursor = connection.execute(
                f"UPDATE coord_workspaces SET {placeholders} WHERE id = ? "
                f"AND state IN ({', '.join('?' for _ in allowed)})",
                (*assignments.values(), str(record.id), *allowed),
            )
            if cursor.rowcount != 1:
                row = connection.execute("SELECT state FROM coord_workspaces WHERE id = ?",
                                         (str(record.id),)).fetchone()
                current = row["state"] if row is not None else "MISSING"
                raise workspace_invalid_state(str(record.id), current, to_state.value.lower())
            event_payload = {"from_state": record.state.value, "to_state": to_state.value,
                             **(payload or {})}
            if detail is not None:
                event_payload["detail"] = detail
            self._journal(connection, record, event, event_payload)
            row = connection.execute("SELECT * FROM coord_workspaces WHERE id = ?",
                                     (str(record.id),)).fetchone()
            return _from_row(row)

    def _set_detail(self, record: StoredWorkspace, detail: str) -> None:
        with self.database.connection(immediate=True) as connection:
            connection.execute(
                "UPDATE coord_workspaces SET detail = ?, updated_at = ? WHERE id = ?",
                (detail, self._clock().isoformat(), str(record.id)),
            )

    def _journal(self, connection: sqlite3.Connection, record: StoredWorkspace,
                 event: JournalEventType, payload: dict[str, Any]) -> None:
        if self.journal is None:
            return
        self.journal.append(
            record.change_id, event, subject_type="workspace", subject_id=record.id,
            payload={"workspace_id": str(record.id), **payload}, connection=connection,
        )

    def _require(self, workspace_id: UUID) -> StoredWorkspace:
        record = self.get(workspace_id)
        if record is None:
            raise workspace_not_found(str(workspace_id))
        return record

    def _repository_lock(self, identity: str) -> threading.Lock:
        with self._locks_guard:
            return self._repository_locks.setdefault(identity, threading.Lock())

    def _workspace_lock(self, workspace_id: UUID) -> threading.Lock:
        with self._locks_guard:
            return self._workspace_locks.setdefault(workspace_id, threading.Lock())


def _opt(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _from_row(row: sqlite3.Row) -> StoredWorkspace:
    capture = row["capture_json"]
    return StoredWorkspace(
        id=UUID(row["id"]),
        change_id=UUID(row["change_id"]),
        purpose=WorkspacePurpose(row["purpose"]),
        repository_identity=row["repository_identity"],
        repository_root=row["repository_root"],
        path=row["path"],
        base_sha=row["base_sha"],
        state=WorkspaceState(row["state"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        attempt_id=UUID(row["attempt_id"]) if row["attempt_id"] else None,
        branch=row["branch"],
        result_sha=row["result_sha"],
        result_ref=row["result_ref"],
        capture=json.loads(capture) if capture else None,
        detail=row["detail"],
        removed_at=_parse_time(row["removed_at"]),
    )


__all__ = ["WorkspaceReconcileReport", "WorkspaceRegistry"]
