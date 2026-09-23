"""Task use cases: request validation, cross-Change checks, and TaskView
composition, over `TaskRepository`'s transactional persistence.

Mirrors `backend/app/core/change_service.py::ChangeService`'s shape: a thin
layer between the HTTP contract and the repository that owns nothing
transactional itself (all atomicity lives in the repository, per
`docs/MULTI_AGENT_IMPLEMENTATION_PLAN.md` section 5's short-transaction rule).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from backend.app.contracts.models import (
    TaskCancelRequest,
    TaskCreateRequest,
    TaskDependenciesRequest,
    TaskEditRequest,
    TaskListResponse,
    TaskSubmitRequest,
    TaskView,
    utc_now,
)
from backend.app.coordination.errors import task_not_found
from backend.app.coordination.models import StoredTask
from backend.app.coordination.repository import TaskRepository


Clock = Callable[[], datetime]


class CoordinationService:
    def __init__(
        self,
        repository: TaskRepository,
        *,
        require_change: Callable[[UUID], Any],
        clock: Clock = utc_now,
    ) -> None:
        self.repository = repository
        # Injected rather than importing ChangeService, so this module has no
        # compile-time dependency on the Change domain beyond "raise
        # CHANGE_NOT_FOUND if this id does not exist" -- matching the plan's
        # "additive" extension-point guidance in section 3. In composition
        # (`backend/app/main.py`) this is bound to `ChangeService.get`, whose
        # return value is discarded here; only its raise-on-miss behavior
        # matters to this service.
        self.require_change = require_change
        self.clock = clock

    def create(
        self,
        change_id: UUID,
        request: TaskCreateRequest,
        *,
        idempotency_key: str | None = None,
    ) -> TaskView:
        self._require_change(change_id)
        now = self.clock()
        stored = self.repository.create(
            StoredTask(
                id=uuid4(),
                change_id=change_id,
                title=request.title,
                instructions=request.instructions,
                adapter=request.adapter,
                created_at=now,
                updated_at=now,
                creator_actor_id=request.creator_actor_id,
                assigned_actor_id=request.assigned_actor_id,
                priority=request.priority,
                max_attempts=request.max_attempts,
                execution_timeout_seconds=request.execution_timeout_seconds,
                executable=request.executable,
                args=tuple(request.args),
                write_paths=tuple(request.write_paths),
                verification=tuple(item.model_dump(mode="json")
                                   for item in request.verification),
                resources=tuple(item.model_dump(mode="json") for item in request.resources),
            ),
            idempotency_key=idempotency_key,
            request_hash=self._request_hash(request),
        )
        return self._to_view(stored)

    def list(self, change_id: UUID, *, limit: int, offset: int) -> TaskListResponse:
        self._require_change(change_id)
        items = [
            self._to_view(item)
            for item in self.repository.list_for_change(
                change_id, limit=limit, offset=offset
            )
        ]
        return TaskListResponse(
            items=items,
            count=len(items),
            total=self.repository.count_for_change(change_id),
        )

    def get(self, change_id: UUID, task_id: UUID) -> TaskView:
        return self._to_view(self._get_stored_in_change(change_id, task_id))

    def edit(
        self,
        change_id: UUID,
        task_id: UUID,
        request: TaskEditRequest,
        *,
        idempotency_key: str | None = None,
    ) -> TaskView:
        self._get_stored_in_change(change_id, task_id)
        fields: dict[str, Any] = {}
        if request.title is not None:
            fields["title"] = request.title
        if request.instructions is not None:
            fields["instructions"] = request.instructions
        if request.adapter is not None:
            fields["adapter"] = request.adapter
        if request.assigned_actor_id is not None:
            fields["assigned_actor_id"] = str(request.assigned_actor_id)
        if request.priority is not None:
            fields["priority"] = request.priority
        if request.max_attempts is not None:
            fields["max_attempts"] = request.max_attempts
        if request.execution_timeout_seconds is not None:
            fields["execution_timeout_seconds"] = request.execution_timeout_seconds
        if request.executable is not None:
            fields["executable"] = request.executable
        if request.args is not None:
            fields["args_json"] = json.dumps(request.args)
        if request.write_paths is not None:
            fields["write_paths_json"] = json.dumps(request.write_paths)
        if request.verification is not None:
            fields["verification_json"] = json.dumps(
                [item.model_dump(mode="json") for item in request.verification])
        if request.resources is not None:
            fields["resources_json"] = json.dumps(
                [item.model_dump(mode="json") for item in request.resources])
        updated = self.repository.edit(
            task_id,
            request.expected_revision,
            self.clock(),
            fields,
            idempotency_key=idempotency_key,
            request_hash=self._request_hash(request),
        )
        if updated is None:
            raise task_not_found(str(task_id))
        return self._to_view(updated)

    def replace_dependencies(
        self,
        change_id: UUID,
        task_id: UUID,
        request: TaskDependenciesRequest,
        *,
        idempotency_key: str | None = None,
    ) -> TaskView:
        self._get_stored_in_change(change_id, task_id)
        updated = self.repository.replace_dependencies(
            task_id,
            change_id,
            request.depends_on_task_ids,
            request.expected_revision,
            self.clock(),
            idempotency_key=idempotency_key,
            request_hash=self._request_hash(request),
        )
        if updated is None:
            raise task_not_found(str(task_id))
        return self._to_view(updated)

    def submit(
        self,
        change_id: UUID,
        task_id: UUID,
        request: TaskSubmitRequest,
        *,
        idempotency_key: str | None = None,
    ) -> TaskView:
        self._get_stored_in_change(change_id, task_id)
        updated = self.repository.submit(
            task_id,
            request.expected_revision,
            self.clock(),
            idempotency_key=idempotency_key,
            request_hash=self._request_hash(request),
        )
        if updated is None:
            raise task_not_found(str(task_id))
        return self._to_view(updated)

    def cancel(
        self,
        change_id: UUID,
        task_id: UUID,
        request: TaskCancelRequest,
        *,
        idempotency_key: str | None = None,
    ) -> TaskView:
        self._get_stored_in_change(change_id, task_id)
        updated = self.repository.cancel(
            task_id,
            request.expected_revision,
            self.clock(),
            request.reason,
            idempotency_key=idempotency_key,
            request_hash=self._request_hash(request),
        )
        if updated is None:
            raise task_not_found(str(task_id))
        return self._to_view(updated)

    # -- internals ----------------------------------------------------------

    def _require_change(self, change_id: UUID) -> None:
        self.require_change(change_id)

    def _get_stored_in_change(self, change_id: UUID, task_id: UUID) -> StoredTask:
        self._require_change(change_id)
        stored = self.repository.get(task_id)
        if stored is None or stored.change_id != change_id:
            raise task_not_found(str(task_id))
        return stored

    @staticmethod
    def _request_hash(value: object) -> str:
        if hasattr(value, "model_dump"):
            payload = value.model_dump(mode="json")  # type: ignore[attr-defined]
        else:
            payload = value
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _to_view(stored: StoredTask) -> TaskView:
        return TaskView(
            id=stored.id,
            change_id=stored.change_id,
            title=stored.title,
            instructions=stored.instructions,
            creator_actor_id=stored.creator_actor_id,
            assigned_actor_id=stored.assigned_actor_id,
            adapter=stored.adapter,
            state=stored.state,
            revision=stored.revision,
            priority=stored.priority,
            max_attempts=stored.max_attempts,
            execution_timeout_seconds=stored.execution_timeout_seconds,
            depends_on_task_ids=list(stored.depends_on_task_ids),
            waiting_reason=stored.waiting_reason,
            failure_reason=stored.failure_reason,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            submitted_at=stored.submitted_at,
            executable=stored.executable,
            args=list(stored.args),
            write_paths=list(stored.write_paths),
            verification=list(stored.verification),
            resources=list(stored.resources),
        )
