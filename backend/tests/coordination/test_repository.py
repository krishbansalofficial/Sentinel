from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.app.contracts.models import TaskState
from backend.app.coordination.models import StoredTask
from backend.app.coordination.repository import TaskRepository
from backend.app.core.database import Database
from backend.app.core.errors import AppError
from backend.app.core.journal import JournalWriter
from backend.migrations.versions import MIGRATIONS


CHANGE_A = uuid4()
CHANGE_B = uuid4()


def _seed_changes(database: Database, *change_ids) -> None:
    """coord_tasks.change_id has an FK to changes(id); seed minimal rows."""

    now = datetime.now(UTC).isoformat()
    with database.connection(immediate=True) as connection:
        for change_id in change_ids:
            connection.execute(
                """
                INSERT INTO changes (
                    id, title, intent, repository_path, created_at, updated_at
                ) VALUES (?, 'T', 'I', 'C:\\work\\repo', ?, ?)
                """,
                (str(change_id), now, now),
            )


def _make_repo(tmp_path, *, with_journal: bool = True) -> TaskRepository:
    database = Database(tmp_path / "coord.sqlite3")
    database.initialize()
    _seed_changes(database, CHANGE_A, CHANGE_B)
    journal = JournalWriter(database) if with_journal else None
    return TaskRepository(database, journal=journal)


def _new_task(change_id, *, title: str = "Do the thing") -> StoredTask:
    now = datetime.now(UTC)
    return StoredTask(
        id=uuid4(), change_id=change_id, title=title,
        instructions="Do it well", adapter="claude",
        created_at=now, updated_at=now,
    )


def test_migration_from_existing_database(tmp_path) -> None:
    """A database that only has migrations 1-10 applied gets coord_tasks/
    coord_dependencies added cleanly by the next `Database.initialize()`,
    with no data loss to the existing `changes` table."""

    path = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    for migration in MIGRATIONS:
        if migration.version >= 11:
            continue
        migration.apply(raw)
        raw.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(UTC).isoformat()),
        )
    raw.execute(
        "INSERT INTO changes (id, title, intent, repository_path, created_at, updated_at) "
        "VALUES (?, 'Existing', 'Pre-coordination change', 'C:\\work\\repo', ?, ?)",
        (str(CHANGE_A), datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    raw.commit()
    raw.close()

    database = Database(path)
    database.initialize()
    assert database.schema_version() == 11

    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "coord_tasks" in tables
    assert "coord_dependencies" in tables

    repository = TaskRepository(database)
    task = repository.create(_new_task(CHANGE_A))
    assert repository.get(task.id) == task


def test_create_persists_across_reopen(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = repository.create(_new_task(CHANGE_A))
    reopened = TaskRepository(Database(repository.database.path))
    assert reopened.get(task.id) == task
    assert reopened.list_for_change(CHANGE_A) == [task]


def test_create_idempotent_replay_and_conflict(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = _new_task(CHANGE_A)
    first = repository.create(task, idempotency_key="key-1", request_hash="hash-a")
    replayed = repository.create(task, idempotency_key="key-1", request_hash="hash-a")
    assert replayed == first
    assert repository.count_for_change(CHANGE_A) == 1

    with pytest.raises(AppError) as excinfo:
        repository.create(task, idempotency_key="key-1", request_hash="hash-b")
    assert excinfo.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_edit_rejects_stale_revision(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = repository.create(_new_task(CHANGE_A))
    repository.edit(task.id, 1, datetime.now(UTC), {"title": "Renamed"})

    with pytest.raises(AppError) as excinfo:
        repository.edit(task.id, 1, datetime.now(UTC), {"title": "Again"})
    assert excinfo.value.code == "TASK_REVISION_CONFLICT"
    assert excinfo.value.details == {"expected_revision": 1, "actual_revision": 2}


def test_dependencies_reject_self_edge(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = repository.create(_new_task(CHANGE_A))
    with pytest.raises(AppError) as excinfo:
        repository.replace_dependencies(
            task.id, CHANGE_A, [task.id], 1, datetime.now(UTC)
        )
    assert excinfo.value.code == "TASK_DEPENDENCY_SELF"


def test_dependencies_reject_cross_change(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task_a = repository.create(_new_task(CHANGE_A))
    task_b = repository.create(_new_task(CHANGE_B))
    with pytest.raises(AppError) as excinfo:
        repository.replace_dependencies(
            task_a.id, CHANGE_A, [task_b.id], 1, datetime.now(UTC)
        )
    assert excinfo.value.code == "TASK_DEPENDENCY_CROSS_CHANGE"


def test_dependencies_reject_unknown_predecessor(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = repository.create(_new_task(CHANGE_A))
    with pytest.raises(AppError) as excinfo:
        repository.replace_dependencies(
            task.id, CHANGE_A, [uuid4()], 1, datetime.now(UTC)
        )
    assert excinfo.value.code == "TASK_DEPENDENCY_NOT_FOUND"


def test_dependencies_reject_two_task_cycle(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task_a = repository.create(_new_task(CHANGE_A, title="A"))
    task_b = repository.create(_new_task(CHANGE_A, title="B"))

    updated_b = repository.replace_dependencies(
        task_b.id, CHANGE_A, [task_a.id], 1, datetime.now(UTC)
    )
    assert updated_b.depends_on_task_ids == (task_a.id,)

    with pytest.raises(AppError) as excinfo:
        repository.replace_dependencies(
            task_a.id, CHANGE_A, [task_b.id], 1, datetime.now(UTC)
        )
    assert excinfo.value.code == "TASK_DEPENDENCY_CYCLE"
    cycle = excinfo.value.details["cycle"]
    assert set(cycle) == {str(task_a.id), str(task_b.id)}

    # The rejected attempt must not have partially applied -- A still has no
    # edges, matching "no queued task launches" style all-or-nothing writes.
    assert repository.get(task_a.id).depends_on_task_ids == ()


def test_dependencies_reject_three_task_cycle(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    a = repository.create(_new_task(CHANGE_A, title="A"))
    b = repository.create(_new_task(CHANGE_A, title="B"))
    c = repository.create(_new_task(CHANGE_A, title="C"))

    repository.replace_dependencies(b.id, CHANGE_A, [a.id], 1, datetime.now(UTC))
    repository.replace_dependencies(c.id, CHANGE_A, [b.id], 1, datetime.now(UTC))

    with pytest.raises(AppError) as excinfo:
        repository.replace_dependencies(a.id, CHANGE_A, [c.id], 1, datetime.now(UTC))
    assert excinfo.value.code == "TASK_DEPENDENCY_CYCLE"


def test_dependencies_and_fields_frozen_after_submit(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = repository.create(_new_task(CHANGE_A))
    repository.submit(task.id, 1, datetime.now(UTC))

    with pytest.raises(AppError) as excinfo:
        repository.edit(task.id, 2, datetime.now(UTC), {"title": "Too late"})
    assert excinfo.value.code == "TASK_NOT_EDITABLE"

    with pytest.raises(AppError) as excinfo:
        repository.replace_dependencies(task.id, CHANGE_A, [], 2, datetime.now(UTC))
    assert excinfo.value.code == "TASK_NOT_EDITABLE"


def test_submit_without_dependencies_is_ready(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = repository.create(_new_task(CHANGE_A))
    updated = repository.submit(task.id, 1, datetime.now(UTC))
    assert updated.state is TaskState.READY
    assert updated.waiting_reason is None
    assert updated.submitted_at is not None


def test_submit_with_unmet_dependency_is_waiting(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    predecessor = repository.create(_new_task(CHANGE_A, title="Predecessor"))
    dependent = repository.create(_new_task(CHANGE_A, title="Dependent"))
    repository.replace_dependencies(
        dependent.id, CHANGE_A, [predecessor.id], 1, datetime.now(UTC)
    )
    updated = repository.submit(dependent.id, 2, datetime.now(UTC))
    assert updated.state is TaskState.WAITING
    assert updated.waiting_reason is not None


def test_submit_twice_is_rejected_as_invalid_transition(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = repository.create(_new_task(CHANGE_A))
    repository.submit(task.id, 1, datetime.now(UTC))
    with pytest.raises(AppError) as excinfo:
        # Correct current revision (2) but the task is READY, not DRAFT.
        repository.submit(task.id, 2, datetime.now(UTC))
    assert excinfo.value.code == "TASK_INVALID_TRANSITION"


def test_cancel_from_ready_and_reject_cancel_of_cancelled(tmp_path) -> None:
    repository = _make_repo(tmp_path)
    task = repository.create(_new_task(CHANGE_A))
    repository.submit(task.id, 1, datetime.now(UTC))
    cancelled = repository.cancel(task.id, 2, datetime.now(UTC), "no longer needed")
    assert cancelled.state is TaskState.CANCELLED
    assert cancelled.failure_reason == "no longer needed"

    with pytest.raises(AppError) as excinfo:
        repository.cancel(task.id, 3, datetime.now(UTC), "again")
    assert excinfo.value.code == "TASK_INVALID_TRANSITION"


def test_domain_and_journal_share_one_transaction_on_injected_failure(tmp_path) -> None:
    """A journal append failure must roll back the paired domain write --
    the task must not exist half-created, mirroring
    test_runtime_service_atomicity.py's proof for the Change domain."""

    class _ExplodingJournal:
        def append(self, *args, **kwargs):
            raise RuntimeError("journal backend unavailable")

    database = Database(tmp_path / "coord.sqlite3")
    database.initialize()
    _seed_changes(database, CHANGE_A)
    repository = TaskRepository(database, journal=_ExplodingJournal())

    task = _new_task(CHANGE_A)
    with pytest.raises(RuntimeError):
        repository.create(task)

    clean_repository = TaskRepository(database)
    assert clean_repository.get(task.id) is None
    assert clean_repository.count_for_change(CHANGE_A) == 0


def test_concurrent_dependency_edits_cannot_create_a_cycle(tmp_path) -> None:
    """Two threads race to complete a cycle from opposite ends at the same
    instant. SQLite's BEGIN IMMEDIATE write lock (Database.connection)
    serializes them; whichever transaction commits second must see the
    edge the first one committed and be rejected -- the graph must never
    observably contain a cycle, checked after both threads finish."""

    repository = _make_repo(tmp_path)
    a = repository.create(_new_task(CHANGE_A, title="A"))
    b = repository.create(_new_task(CHANGE_A, title="B"))

    barrier = threading.Barrier(2)
    outcomes: dict[str, Exception | None] = {}

    def make_b_depend_on_a() -> None:
        barrier.wait()
        try:
            repository.replace_dependencies(b.id, CHANGE_A, [a.id], 1, datetime.now(UTC))
            outcomes["b_on_a"] = None
        except AppError as error:
            outcomes["b_on_a"] = error

    def make_a_depend_on_b() -> None:
        barrier.wait()
        try:
            repository.replace_dependencies(a.id, CHANGE_A, [b.id], 1, datetime.now(UTC))
            outcomes["a_on_b"] = None
        except AppError as error:
            outcomes["a_on_b"] = error

    t1 = threading.Thread(target=make_b_depend_on_a)
    t2 = threading.Thread(target=make_a_depend_on_b)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Exactly one edge direction can win; the other either lost the
    # revision race (stale expected_revision=1 after the first committed)
    # or was rejected for completing a cycle. Both are legitimate outcomes
    # of a real interleaving -- the invariant under test is what follows.
    final_a = repository.get(a.id)
    final_b = repository.get(b.id)
    both_have_edges = bool(final_a.depends_on_task_ids) and bool(final_b.depends_on_task_ids)
    assert not both_have_edges, "both directions applied: the graph has a cycle"
