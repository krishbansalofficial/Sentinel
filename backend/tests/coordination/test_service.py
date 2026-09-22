from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.app.contracts.models import (
    TaskCancelRequest,
    TaskCreateRequest,
    TaskDependenciesRequest,
    TaskEditRequest,
    TaskState,
    TaskSubmitRequest,
)
from backend.app.core.errors import AppError, change_not_found
from backend.app.coordination.repository import TaskRepository
from backend.app.coordination.service import CoordinationService
from backend.app.core.database import Database


CHANGE_ID = uuid4()
OTHER_CHANGE_ID = uuid4()


def _seed_changes(database: Database, *change_ids) -> None:
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


def _known_changes(*known_ids):
    known = set(known_ids)

    def require_change(change_id):
        if change_id not in known:
            raise change_not_found(str(change_id))

    return require_change


def _make_service(tmp_path) -> CoordinationService:
    database = Database(tmp_path / "coord.sqlite3")
    database.initialize()
    _seed_changes(database, CHANGE_ID, OTHER_CHANGE_ID)
    return CoordinationService(
        TaskRepository(database),
        require_change=_known_changes(CHANGE_ID, OTHER_CHANGE_ID),
    )


def _create_request(**overrides) -> TaskCreateRequest:
    payload = {
        "title": "Implement the thing",
        "instructions": "Do it carefully",
        "adapter": "claude",
    }
    payload.update(overrides)
    return TaskCreateRequest(**payload)


def test_create_requires_a_real_change(tmp_path) -> None:
    service = _make_service(tmp_path)
    with pytest.raises(AppError) as excinfo:
        service.create(uuid4(), _create_request())
    assert excinfo.value.code == "CHANGE_NOT_FOUND"


def test_create_list_get_roundtrip(tmp_path) -> None:
    service = _make_service(tmp_path)
    created = service.create(CHANGE_ID, _create_request())
    assert created.state is TaskState.DRAFT
    assert created.revision == 1

    listed = service.list(CHANGE_ID, limit=100, offset=0)
    assert listed.count == 1
    assert listed.items[0].id == created.id

    fetched = service.get(CHANGE_ID, created.id)
    assert fetched == created


def test_get_task_from_wrong_change_is_not_found(tmp_path) -> None:
    service = _make_service(tmp_path)
    created = service.create(CHANGE_ID, _create_request())
    with pytest.raises(AppError) as excinfo:
        service.get(OTHER_CHANGE_ID, created.id)
    assert excinfo.value.code == "TASK_NOT_FOUND"


def test_full_lifecycle_create_edit_dependencies_submit_cancel(tmp_path) -> None:
    service = _make_service(tmp_path)
    predecessor = service.create(CHANGE_ID, _create_request(title="First"))
    dependent = service.create(CHANGE_ID, _create_request(title="Second"))

    edited = service.edit(
        CHANGE_ID, dependent.id,
        TaskEditRequest(expected_revision=1, priority=5),
    )
    assert edited.priority == 5
    assert edited.revision == 2

    wired = service.replace_dependencies(
        CHANGE_ID, dependent.id,
        TaskDependenciesRequest(
            expected_revision=2, depends_on_task_ids=[predecessor.id]
        ),
    )
    assert wired.depends_on_task_ids == [predecessor.id]

    submitted = service.submit(
        CHANGE_ID, dependent.id, TaskSubmitRequest(expected_revision=3)
    )
    assert submitted.state is TaskState.WAITING

    cancelled = service.cancel(
        CHANGE_ID, dependent.id,
        TaskCancelRequest(expected_revision=4, reason="scope cut"),
    )
    assert cancelled.state is TaskState.CANCELLED
    assert cancelled.failure_reason == "scope cut"


def test_idempotent_create_replay_returns_same_task(tmp_path) -> None:
    service = _make_service(tmp_path)
    request = _create_request()
    first = service.create(CHANGE_ID, request, idempotency_key="create-1")
    second = service.create(CHANGE_ID, request, idempotency_key="create-1")
    assert first == second
    assert service.list(CHANGE_ID, limit=100, offset=0).total == 1


def test_idempotent_create_conflict_on_changed_payload(tmp_path) -> None:
    service = _make_service(tmp_path)
    service.create(CHANGE_ID, _create_request(title="Original"), idempotency_key="create-2")
    with pytest.raises(AppError) as excinfo:
        service.create(
            CHANGE_ID, _create_request(title="Different"), idempotency_key="create-2"
        )
    assert excinfo.value.code == "IDEMPOTENCY_KEY_REUSED"
