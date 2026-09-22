"""Internal persistence-shaped records for the coordination domain.

Mirrors `backend/app/core/change_repository.py::StoredChange`: a plain frozen
dataclass the repository reads/writes, kept separate from the public
`TaskView` contract model so storage shape and API shape can evolve
independently (`TaskView` has no `enqueue_seq`, for instance -- that field is
scheduler-internal, added in Phase 3, and never exposed today).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from typing import Any

from backend.app.contracts.models import TaskState, WorkspacePurpose, WorkspaceState


@dataclass(frozen=True, slots=True)
class StoredTask:
    id: UUID
    change_id: UUID
    title: str
    instructions: str
    adapter: str
    created_at: datetime
    updated_at: datetime
    creator_actor_id: UUID | None = None
    assigned_actor_id: UUID | None = None
    state: TaskState = TaskState.DRAFT
    revision: int = 1
    priority: int = 0
    enqueue_seq: int = 0
    max_attempts: int = 3
    execution_timeout_seconds: int = 900
    waiting_reason: str | None = None
    failure_reason: str | None = None
    submitted_at: datetime | None = None
    depends_on_task_ids: tuple[UUID, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class StoredWorkspace:
    """One `coord_workspaces` row. `capture` is the immutable manifest written
    when the result was committed (files, scope violations, exclusions)."""

    id: str
    change_id: UUID
    purpose: WorkspacePurpose
    repository_identity: str
    repository_root: str
    path: str
    base_sha: str
    state: WorkspaceState
    created_at: datetime
    updated_at: datetime
    attempt_id: UUID | None = None
    branch: str | None = None
    result_sha: str | None = None
    result_ref: str | None = None
    capture: dict[str, Any] | None = None
    detail: str | None = None
    removed_at: datetime | None = None
