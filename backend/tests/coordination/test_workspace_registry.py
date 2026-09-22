"""Phase 2: durable workspace records, retention states, and crash recovery.

Real temporary Git repositories and a real SQLite database throughout. Crashes
are injected by raising `SimulatedCrash` (a `BaseException`, so no `except
Exception` cleanup path can intercept it, exactly like a killed process)
between the database write and the Git side effect in both directions.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from backend.app.contracts.models import JournalEventType, WorkspacePurpose, WorkspaceState
from backend.app.coordination.workspace_registry import WorkspaceRegistry
from backend.app.coordination.workspaces import WorkspaceManager
from backend.app.core.database import Database
from backend.app.core.errors import AppError
from backend.app.core.journal import JournalWriter
from backend.app.core.replay_service import ReplayService
from backend.migrations import LATEST_SCHEMA_VERSION
from backend.migrations.versions import MIGRATIONS


CHANGE = uuid4()
OTHER_CHANGE = uuid4()


class SimulatedCrash(BaseException):
    """Stands in for the process dying mid-operation."""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "user repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.autocrlf", "false")
    (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def _seed_changes(database: Database, *change_ids: UUID) -> None:
    now = datetime.now(UTC).isoformat()
    with database.connection(immediate=True) as connection:
        for change_id in change_ids:
            connection.execute(
                "INSERT INTO changes (id, title, intent, repository_path, created_at, updated_at) "
                "VALUES (?, 'T', 'I', 'C:\\work\\repo', ?, ?)",
                (str(change_id), now, now),
            )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state" / "coord.sqlite3")
    db.initialize()
    _seed_changes(db, CHANGE, OTHER_CHANGE)
    return db


@pytest.fixture
def manager(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "state" / "coord-workspaces")


@pytest.fixture
def registry(database: Database, manager: WorkspaceManager) -> WorkspaceRegistry:
    return WorkspaceRegistry(database, manager, journal=JournalWriter(database))


def _events(database: Database, change_id: UUID = CHANGE) -> list[tuple[str, str | None]]:
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT event_type, subject_id FROM journal_events WHERE change_id = ? ORDER BY seq",
            (str(change_id),),
        ).fetchall()
    return [(row["event_type"], row["subject_id"]) for row in rows]


def _event_types(database: Database, subject: UUID) -> list[str]:
    return [kind for kind, subject_id in _events(database) if subject_id == str(subject)]


def _provision(registry: WorkspaceRegistry, repo: Path, **overrides) -> tuple[UUID, object]:
    workspace_id = overrides.pop("workspace_id", uuid4())
    kwargs = dict(workspace_id=workspace_id, purpose=WorkspacePurpose.ATTEMPT,
                  base_revision="main")
    kwargs.update(overrides)
    return workspace_id, registry.provision(CHANGE, str(repo), **kwargs)


# -- happy path ---------------------------------------------------------------


def test_provision_records_ready_workspace_pinned_to_base(
    registry: WorkspaceRegistry, database: Database, repo: Path
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    attempt = uuid4()
    workspace_id, record = _provision(registry, repo, attempt_id=attempt)

    assert record.state is WorkspaceState.READY
    assert record.base_sha == base
    assert record.branch == f"sentinel/attempt/{workspace_id}"
    assert record.attempt_id == attempt
    assert Path(record.path).is_dir()
    assert git(Path(record.path), "rev-parse", "HEAD") == base
    assert _event_types(database, workspace_id) == [
        JournalEventType.WORKSPACE_CREATING.value, JournalEventType.WORKSPACE_READY.value,
    ]
    assert registry.get(workspace_id) == record
    assert registry.for_attempt(attempt) == [record]


def test_integration_workspace_is_detached(registry: WorkspaceRegistry, repo: Path) -> None:
    _, record = _provision(registry, repo, purpose=WorkspacePurpose.INTEGRATION)
    assert record.branch is None
    assert git(Path(record.path), "branch", "--show-current") == ""


def test_capture_pins_result_and_manifest_survives_restart(
    registry: WorkspaceRegistry, database: Database, manager: WorkspaceManager, repo: Path
) -> None:
    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    (Path(record.path) / ".env").write_text("SECRET=1\n", encoding="utf-8")

    captured = registry.capture(workspace_id, write_paths=["*.py"], message="attempt 1")

    assert captured.state is WorkspaceState.CAPTURED
    assert captured.result_ref == f"refs/heads/sentinel/results/{workspace_id}"
    assert git(repo, "rev-parse", captured.result_ref) == captured.result_sha
    assert captured.capture["files"] == [
        {"path": "app.py", "status": "modified", "old_path": None, "binary": False}]
    assert captured.capture["excluded_sensitive"] == [".env"]
    assert captured.capture["scope_violations"] == []
    # A brand-new registry over the same database sees the same durable record.
    reopened = WorkspaceRegistry(database, manager)
    assert reopened.get(workspace_id) == captured


def test_phase2_exit_two_attempts_same_file_user_untouched_through_registry(
    registry: WorkspaceRegistry, repo: Path
) -> None:
    """Plan section 14 Phase 2 exit, exercised through the durable layer:
    two attempts edit the same relative file without changing each other's
    working file or the user's branch, and both results outlive cleanup."""

    base = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("USER DIRTY\n", encoding="utf-8")
    first_id, first = _provision(registry, repo)
    second_id, second = _provision(registry, repo)
    (Path(first.path) / "app.py").write_text("one\n", encoding="utf-8")
    (Path(second.path) / "app.py").write_text("two\n", encoding="utf-8")
    assert (Path(first.path) / "app.py").read_text(encoding="utf-8") == "one\n"

    one = registry.capture(first_id, write_paths=[], message="one")
    two = registry.capture(second_id, write_paths=[], message="two")
    registry.remove(first_id, attempt_live=False)
    registry.remove(second_id, attempt_live=False)
    git(repo, "gc", "-q", "--prune=now")

    assert one.result_sha != two.result_sha
    assert git(repo, "show", f"{one.result_ref}:app.py") == "one"
    assert git(repo, "show", f"{two.result_ref}:app.py") == "two"
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == base
    assert (repo / "app.py").read_text(encoding="utf-8") == "USER DIRTY\n"


def test_list_for_change_is_scoped(registry: WorkspaceRegistry, repo: Path) -> None:
    first_id, _ = _provision(registry, repo)
    registry.provision(OTHER_CHANGE, str(repo), workspace_id=uuid4(),
                       purpose=WorkspacePurpose.ATTEMPT, base_revision="main")
    assert [item.id for item in registry.list_for_change(CHANGE)] == [first_id]
    assert len(registry.list_for_change(OTHER_CHANGE)) == 1


def test_journal_chain_stays_verifiable(
    registry: WorkspaceRegistry, database: Database, repo: Path
) -> None:
    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / "x.txt").write_text("x\n", encoding="utf-8")
    registry.capture(workspace_id, write_paths=[], message="x")
    registry.remove(workspace_id, attempt_live=False)

    assert _event_types(database, workspace_id) == [
        "workspace.creating", "workspace.ready", "workspace.captured",
        "workspace.removing", "workspace.removed",
    ]
    assert ReplayService(database).verify_chain(CHANGE).verified is True


# -- refusals -------------------------------------------------------------------


def test_duplicate_workspace_id_is_refused_and_original_untouched(
    registry: WorkspaceRegistry, repo: Path
) -> None:
    workspace_id, original = _provision(registry, repo)
    with pytest.raises(AppError) as error:
        _provision(registry, repo, workspace_id=workspace_id)
    assert error.value.code == "WORKSPACE_EXISTS"
    assert registry.get(workspace_id) == original
    assert Path(original.path).is_dir()


def test_unresolvable_base_writes_no_record(
    registry: WorkspaceRegistry, database: Database, repo: Path
) -> None:
    workspace_id = uuid4()
    with pytest.raises(AppError) as error:
        _provision(registry, repo, workspace_id=workspace_id, base_revision="no-such-branch")
    assert error.value.code == "WORKSPACE_BASE_UNRESOLVED"
    assert registry.get(workspace_id) is None
    assert _events(database) == []


def test_git_failure_during_create_marks_failed(
    registry: WorkspaceRegistry, database: Database, repo: Path
) -> None:
    workspace_id = uuid4()
    git(repo, "branch", f"sentinel/attempt/{workspace_id}")  # branch name already taken

    with pytest.raises(AppError):
        _provision(registry, repo, workspace_id=workspace_id)

    record = registry.get(workspace_id)
    assert record.state is WorkspaceState.FAILED
    assert record.detail.startswith("create: ")
    assert _event_types(database, workspace_id)[-1] == "workspace.failed"


def test_capture_and_remove_of_unknown_workspace_is_not_found(
    registry: WorkspaceRegistry,
) -> None:
    for call in (lambda: registry.capture(uuid4(), write_paths=[], message="m"),
                 lambda: registry.remove(uuid4(), attempt_live=False)):
        with pytest.raises(AppError) as error:
            call()
        assert error.value.code == "WORKSPACE_NOT_FOUND"
        assert error.value.status_code == 404


def test_ready_workspace_is_never_removed(registry: WorkspaceRegistry, repo: Path) -> None:
    """READY may hold agent work nobody captured; retention forbids removal."""

    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / "unsaved.txt").write_text("agent work\n", encoding="utf-8")
    with pytest.raises(AppError) as error:
        registry.remove(workspace_id, attempt_live=False)
    assert error.value.code == "WORKSPACE_INVALID_STATE"
    assert (Path(record.path) / "unsaved.txt").exists()
    assert registry.get(workspace_id).state is WorkspaceState.READY


def test_live_attempt_workspace_is_never_removed(registry: WorkspaceRegistry, repo: Path) -> None:
    workspace_id, record = _provision(registry, repo)
    registry.capture(workspace_id, write_paths=[], message="m")
    with pytest.raises(AppError) as error:
        registry.remove(workspace_id, attempt_live=True)
    assert error.value.code == "WORKSPACE_ACTIVE"
    assert Path(record.path).is_dir()
    assert registry.get(workspace_id).state is WorkspaceState.CAPTURED


def test_removal_is_idempotent_and_keeps_the_result(
    registry: WorkspaceRegistry, database: Database, repo: Path
) -> None:
    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / "r.txt").write_text("r\n", encoding="utf-8")
    captured = registry.capture(workspace_id, write_paths=[], message="r")

    removed = registry.remove(workspace_id, attempt_live=False)
    again = registry.remove(workspace_id, attempt_live=False)

    assert removed.state is WorkspaceState.REMOVED and removed.removed_at is not None
    assert again == removed
    assert not Path(record.path).exists()
    assert git(repo, "rev-parse", captured.result_ref) == captured.result_sha
    assert _event_types(database, workspace_id).count("workspace.removed") == 1
    with pytest.raises(AppError) as error:
        registry.capture(workspace_id, write_paths=[], message="late")
    assert error.value.code == "WORKSPACE_INVALID_STATE"


def test_failed_create_over_foreign_directory_never_deletes_it(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path
) -> None:
    """A directory already sitting at the managed path is not Sentinel's.
    Creation fails, and cleanup of the FAILED record must leave it alone."""

    workspace_id = uuid4()
    squatter = manager.managed_root / str(workspace_id)
    squatter.mkdir()
    (squatter / "precious.txt").write_text("not yours\n", encoding="utf-8")

    with pytest.raises(AppError) as create_error:
        _provision(registry, repo, workspace_id=workspace_id)
    assert create_error.value.code == "WORKSPACE_EXISTS"
    assert registry.get(workspace_id).state is WorkspaceState.FAILED

    with pytest.raises(AppError) as remove_error:
        registry.remove(workspace_id, attempt_live=False)
    assert remove_error.value.code == "WORKSPACE_UNOWNED_DIRECTORY"
    assert (squatter / "precious.txt").read_text(encoding="utf-8") == "not yours\n"
    record = registry.get(workspace_id)
    assert record.state is WorkspaceState.REMOVING
    assert "not a registered worktree" in record.detail


def test_failed_workspace_without_directory_is_removed(
    registry: WorkspaceRegistry, repo: Path
) -> None:
    workspace_id = uuid4()
    git(repo, "branch", f"sentinel/attempt/{workspace_id}")
    with pytest.raises(AppError):
        _provision(registry, repo, workspace_id=workspace_id)
    assert registry.remove(workspace_id, attempt_live=False).state is WorkspaceState.REMOVED


def test_corrupt_workspace_capture_marks_failed(registry: WorkspaceRegistry, repo: Path) -> None:
    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / ".git").unlink()
    with pytest.raises(AppError) as error:
        registry.capture(workspace_id, write_paths=[], message="m")
    assert error.value.code == "WORKSPACE_CORRUPT"
    stored = registry.get(workspace_id)
    assert stored.state is WorkspaceState.FAILED
    assert stored.detail == "capture: WORKSPACE_CORRUPT"


def test_diverged_capture_leaves_workspace_ready_for_inspection(
    registry: WorkspaceRegistry, repo: Path
) -> None:
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    workspace_id, record = _provision(registry, repo)
    git(Path(record.path), "reset", "-q", "--hard", "HEAD~1")
    with pytest.raises(AppError) as error:
        registry.capture(workspace_id, write_paths=[], message="m")
    assert error.value.code == "WORKSPACE_BASE_DIVERGED"
    assert registry.get(workspace_id).state is WorkspaceState.READY


# -- idempotency and atomicity -----------------------------------------------------


def test_capture_is_idempotent_and_never_mints_a_second_commit(
    registry: WorkspaceRegistry, database: Database, repo: Path
) -> None:
    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / "app.py").write_text("once\n", encoding="utf-8")
    first = registry.capture(workspace_id, write_paths=[], message="first")
    (Path(record.path) / "late.txt").write_text("written after capture\n", encoding="utf-8")
    second = registry.capture(workspace_id, write_paths=[], message="second")

    assert second == first
    assert git(repo, "rev-parse", record.branch) == first.result_sha
    assert _event_types(database, workspace_id).count("workspace.captured") == 1


def test_journal_failure_rolls_back_the_capture_transition(
    registry: WorkspaceRegistry, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Domain state and its journal event commit together or not at all; the
    retry then completes from the already-committed Git result."""

    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / "app.py").write_text("atomic\n", encoding="utf-8")
    real_append = registry.journal.append

    def failing_append(change_id, event_type, **kwargs):
        if event_type is JournalEventType.WORKSPACE_CAPTURED:
            raise RuntimeError("journal unavailable")
        return real_append(change_id, event_type, **kwargs)

    monkeypatch.setattr(registry.journal, "append", failing_append)
    with pytest.raises(RuntimeError):
        registry.capture(workspace_id, write_paths=[], message="m")
    stored = registry.get(workspace_id)
    assert stored.state is WorkspaceState.READY
    assert stored.result_sha is None and stored.capture is None

    monkeypatch.setattr(registry.journal, "append", real_append)
    captured = registry.capture(workspace_id, write_paths=[], message="m")
    assert captured.state is WorkspaceState.CAPTURED
    assert git(repo, "rev-list", "--count", f"{record.base_sha}..{captured.result_sha}") == "1"


# -- crash injection and reconciliation ------------------------------------------------


def test_crash_before_git_create_reconciles_to_failed(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(*_args, **_kwargs):
        raise SimulatedCrash

    monkeypatch.setattr(manager, "create", crash)
    workspace_id = uuid4()
    with pytest.raises(SimulatedCrash):
        _provision(registry, repo, workspace_id=workspace_id)
    assert registry.get(workspace_id).state is WorkspaceState.CREATING
    monkeypatch.undo()

    report = registry.reconcile()

    assert report.failed == [workspace_id]
    assert registry.get(workspace_id).detail == "reconcile: creation was interrupted"


def test_crash_after_git_create_reconciles_to_ready(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_create = manager.create

    def create_then_crash(*args, **kwargs):
        real_create(*args, **kwargs)
        raise SimulatedCrash

    monkeypatch.setattr(manager, "create", create_then_crash)
    workspace_id = uuid4()
    with pytest.raises(SimulatedCrash):
        _provision(registry, repo, workspace_id=workspace_id)
    monkeypatch.undo()

    report = registry.reconcile()

    assert report.readied == [workspace_id]
    record = registry.get(workspace_id)
    assert record.state is WorkspaceState.READY
    # The reconciled workspace is fully usable.
    (Path(record.path) / "after.txt").write_text("ok\n", encoding="utf-8")
    assert registry.capture(workspace_id, write_paths=[], message="m").state \
        is WorkspaceState.CAPTURED


def test_creating_row_whose_worktree_moved_off_base_is_failed(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worktree that exists but is not at the recorded base cannot be
    proven to be the one this row intended, so it is not promoted."""

    real_create = manager.create

    def create_move_then_crash(root, workspace_id, base_sha, branch):
        path = real_create(root, workspace_id, base_sha, branch)
        (path / "moved.txt").write_text("m\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "moved")
        raise SimulatedCrash

    monkeypatch.setattr(manager, "create", create_move_then_crash)
    workspace_id = uuid4()
    with pytest.raises(SimulatedCrash):
        _provision(registry, repo, workspace_id=workspace_id)
    monkeypatch.undo()

    assert registry.reconcile().failed == [workspace_id]


def test_crash_after_capture_commit_completes_on_retry_without_second_commit(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / "app.py").write_text("crashy\n", encoding="utf-8")

    def crash(*_args, **_kwargs):
        raise SimulatedCrash

    monkeypatch.setattr(manager, "pin_result", crash)
    with pytest.raises(SimulatedCrash):
        registry.capture(workspace_id, write_paths=[], message="m")
    monkeypatch.undo()
    assert registry.get(workspace_id).state is WorkspaceState.READY
    committed = git(repo, "rev-parse", record.branch)

    captured = registry.capture(workspace_id, write_paths=[], message="m")

    assert captured.result_sha == committed
    assert captured.capture["no_change"] is False
    assert [f["path"] for f in captured.capture["files"]] == ["app.py"]


def test_crash_before_git_removal_is_pending_then_retry_completes(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, record = _provision(registry, repo)
    registry.capture(workspace_id, write_paths=[], message="m")

    def crash(*_args, **_kwargs):
        raise SimulatedCrash

    monkeypatch.setattr(manager, "remove", crash)
    with pytest.raises(SimulatedCrash):
        registry.remove(workspace_id, attempt_live=False)
    monkeypatch.undo()
    assert registry.get(workspace_id).state is WorkspaceState.REMOVING

    assert registry.reconcile().pending_removal == [workspace_id]
    assert registry.remove(workspace_id, attempt_live=False).state is WorkspaceState.REMOVED
    assert not Path(record.path).exists()


def test_crash_after_git_removal_reconciles_to_removed(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, _ = _provision(registry, repo)
    registry.capture(workspace_id, write_paths=[], message="m")
    real_remove = manager.remove

    def remove_then_crash(*args, **kwargs):
        real_remove(*args, **kwargs)
        raise SimulatedCrash

    monkeypatch.setattr(manager, "remove", remove_then_crash)
    with pytest.raises(SimulatedCrash):
        registry.remove(workspace_id, attempt_live=False)
    monkeypatch.undo()

    report = registry.reconcile()

    assert report.removed == [workspace_id]
    assert registry.get(workspace_id).state is WorkspaceState.REMOVED


def test_reconcile_settles_externally_deleted_directories(
    registry: WorkspaceRegistry, repo: Path
) -> None:
    import shutil

    ready_id, ready = _provision(registry, repo)
    captured_id, captured = _provision(registry, repo)
    registry.capture(captured_id, write_paths=[], message="m")
    shutil.rmtree(ready.path)
    shutil.rmtree(captured.path)

    report = registry.reconcile()

    assert report.failed == [ready_id]
    assert report.removed == [captured_id]
    assert "uncaptured work lost" in registry.get(ready_id).detail
    assert registry.get(captured_id).result_ref is not None


def test_reconcile_reports_orphans_but_never_deletes_them(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path
) -> None:
    kept_id, kept = _provision(registry, repo)
    orphan = manager.managed_root / "left-behind"
    orphan.mkdir()
    (orphan / "data.txt").write_text("d\n", encoding="utf-8")

    report = registry.reconcile()

    assert report.orphan_paths == [str(orphan)]
    assert (orphan / "data.txt").exists()
    assert report.readied == report.failed == report.removed == []
    assert registry.get(kept_id) == kept


def test_reconcile_is_a_no_op_when_everything_is_settled(
    registry: WorkspaceRegistry, database: Database, repo: Path
) -> None:
    workspace_id, _ = _provision(registry, repo)
    registry.capture(workspace_id, write_paths=[], message="m")
    before = _events(database)
    report = registry.reconcile()
    assert (report.readied, report.failed, report.removed, report.pending_removal,
            report.orphan_paths) == ([], [], [], [], [])
    assert _events(database) == before


# -- concurrency --------------------------------------------------------------------


def _run_concurrently(count: int, work) -> tuple[list[object], list[BaseException]]:
    barrier = threading.Barrier(count)
    results: list[object] = [None] * count
    errors: list[BaseException] = []

    def runner(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[index] = work(index)
        except BaseException as exc:  # surfaced to the caller
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    return results, errors


def test_concurrent_provisioning_on_one_repository(
    registry: WorkspaceRegistry, manager: WorkspaceManager, repo: Path
) -> None:
    ids = [uuid4() for _ in range(6)]
    results, errors = _run_concurrently(len(ids), lambda i: registry.provision(
        CHANGE, str(repo), workspace_id=ids[i], purpose=WorkspacePurpose.ATTEMPT,
        base_revision="main"))

    assert errors == []
    assert all(record.state is WorkspaceState.READY for record in results)
    registered = manager.registered_worktrees(str(repo))
    assert all(WorkspaceManager._canonical(Path(r.path)) in registered for r in results)


def test_concurrent_capture_of_one_workspace_yields_one_result(
    registry: WorkspaceRegistry, database: Database, repo: Path
) -> None:
    workspace_id, record = _provision(registry, repo)
    (Path(record.path) / "app.py").write_text("raced\n", encoding="utf-8")

    results, errors = _run_concurrently(4, lambda _i: registry.capture(
        workspace_id, write_paths=[], message="race"))

    assert errors == []
    assert len({r.result_sha for r in results}) == 1
    assert _event_types(database, workspace_id).count("workspace.captured") == 1
    assert git(repo, "rev-list", "--count", f"{record.base_sha}..{record.branch}") == "1"


def test_two_registries_race_to_capture_one_workspace(
    database: Database, manager: WorkspaceManager, repo: Path
) -> None:
    """In-process locks do not span registries; the database state guard
    must still let exactly one capture transition win."""

    first = WorkspaceRegistry(database, manager, journal=JournalWriter(database))
    second = WorkspaceRegistry(database, manager, journal=JournalWriter(database))
    workspace_id, record = _provision(first, repo)
    (Path(record.path) / "app.py").write_text("two registries\n", encoding="utf-8")
    registries = [first, second]

    results, errors = _run_concurrently(2, lambda i: registries[i].capture(
        workspace_id, write_paths=[], message="race"))

    winners = [r for r in results if r is not None]
    assert winners, errors
    assert _event_types(database, workspace_id).count("workspace.captured") == 1
    assert first.get(workspace_id).state is WorkspaceState.CAPTURED
    # A loser, if any, failed loudly rather than recording a second result.
    for error in errors:
        assert isinstance(error, AppError), error


# -- migration ----------------------------------------------------------------------


def test_migration_13_upgrades_existing_workspace_rows(tmp_path: Path) -> None:
    path = tmp_path / "v12.sqlite3"
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    for migration in MIGRATIONS:
        if migration.version > 12:
            break
        migration.apply(raw)
        raw.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                    (migration.version, migration.name, datetime.now(UTC).isoformat()))
    now = datetime.now(UTC).isoformat()
    raw.execute("INSERT INTO changes (id, title, intent, repository_path, created_at, updated_at) "
                "VALUES (?, 'T', 'I', 'C:\\r', ?, ?)", (str(CHANGE), now, now))
    workspace_id = uuid4()
    raw.execute(
        "INSERT INTO coord_workspaces (id, change_id, purpose, repository_identity, "
        "repository_root, path, base_sha, state, created_at, updated_at) "
        "VALUES (?, ?, 'ATTEMPT', 'id', 'root', 'p', ?, 'READY', ?, ?)",
        (str(workspace_id), str(CHANGE), "a" * 40, now, now),
    )
    raw.commit()
    raw.close()

    database = Database(path)
    database.initialize()

    assert database.schema_version() == LATEST_SCHEMA_VERSION >= 13
    record = WorkspaceRegistry(database, WorkspaceManager(tmp_path / "m")).get(workspace_id)
    assert record.state is WorkspaceState.READY
    assert record.capture is None and record.result_ref is None and record.removed_at is None
