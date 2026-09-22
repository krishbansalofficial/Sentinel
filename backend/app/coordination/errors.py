"""Stable `AppError` codes for the coordination (task/dependency) domain.

Mirrors `backend/app/core/errors.py`'s shape and conventions exactly, so the
existing `AppError`/`ErrorEnvelope` exception handler in `backend/app/main.py`
needs no changes to serve these.
"""

from __future__ import annotations

from backend.app.core.errors import AppError


def task_not_found(task_id: str) -> AppError:
    return AppError(
        "TASK_NOT_FOUND",
        "The requested task does not exist.",
        status_code=404,
        details={"task_id": task_id},
    )


def task_revision_conflict(*, expected: int, actual: int) -> AppError:
    return AppError(
        "TASK_REVISION_CONFLICT",
        "The task was updated by another operation.",
        status_code=409,
        details={"expected_revision": expected, "actual_revision": actual},
    )


def task_not_editable(task_id: str, state: str) -> AppError:
    return AppError(
        "TASK_NOT_EDITABLE",
        "The task's fields and dependencies may only change while it is DRAFT.",
        status_code=409,
        details={"task_id": task_id, "state": state},
    )


def task_dependency_self(task_id: str) -> AppError:
    return AppError(
        "TASK_DEPENDENCY_SELF",
        "A task cannot depend on itself.",
        status_code=409,
        details={"task_id": task_id},
    )


def task_dependency_cross_change(task_id: str, depends_on_task_id: str) -> AppError:
    return AppError(
        "TASK_DEPENDENCY_CROSS_CHANGE",
        "A task may only depend on other tasks in the same Change.",
        status_code=409,
        details={"task_id": task_id, "depends_on_task_id": depends_on_task_id},
    )


def task_dependency_not_found(depends_on_task_id: str) -> AppError:
    return AppError(
        "TASK_DEPENDENCY_NOT_FOUND",
        "A referenced predecessor task does not exist in this Change.",
        status_code=409,
        details={"depends_on_task_id": depends_on_task_id},
    )


def task_dependency_cycle(cycle: list[str]) -> AppError:
    return AppError(
        "TASK_DEPENDENCY_CYCLE",
        "This dependency edit would create a cycle in the task graph.",
        status_code=409,
        details={"cycle": cycle},
    )


def task_invalid_transition(task_id: str, current: str, action: str) -> AppError:
    return AppError(
        "TASK_INVALID_TRANSITION",
        f"The task cannot be {action} from its current state.",
        status_code=409,
        details={"task_id": task_id, "current_state": current, "action": action},
    )


def workspace_not_found(workspace_id: str) -> AppError:
    return AppError(
        "WORKSPACE_NOT_FOUND",
        "The requested workspace does not exist.",
        status_code=404,
        details={"workspace_id": workspace_id},
    )


def workspace_invalid_state(workspace_id: str, current: str, action: str) -> AppError:
    return AppError(
        "WORKSPACE_INVALID_STATE",
        f"The workspace cannot be {action} from its current state.",
        status_code=409,
        details={"workspace_id": workspace_id, "current_state": current, "action": action},
    )
