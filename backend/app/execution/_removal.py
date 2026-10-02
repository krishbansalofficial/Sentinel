"""Retry a no-follow tree removal through transient Windows sharing/access errors.

Handles close late after a Job Object is terminated, so access-denied (5) and
sharing-violation (32) errors are retried with a growing delay; any other
error, or the last attempt's, is raised unchanged. Shared by the check box and
workspace cleanups so their retry semantics cannot drift apart.
"""

from __future__ import annotations

import time
from collections.abc import Callable

TRANSIENT_WINERRORS = frozenset({5, 32})  # access denied, sharing violation


def is_transient(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in TRANSIENT_WINERRORS


def remove_with_retries(
    remove: Callable[[], None], *, attempts: int, backoff_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            remove()
            return
        except OSError as exc:
            if not is_transient(exc) or attempt == attempts:
                raise
            sleep(backoff_seconds * attempt)
