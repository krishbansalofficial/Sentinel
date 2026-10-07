"""A worker pool over :class:`queue.JobQueue`: N workers claim, run, heartbeat and complete.

Each worker is a thread (``run_pool``) or a separate process (``worker_main``, for
crash tests and real isolation). A heartbeat thread keeps the lease alive while
the handler runs; if the lease is lost the result is dropped by fencing. Handler
exceptions release the job back to the queue (FAILED after ``max_claims``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.app.evals.queue import DEFAULT_LEASE_SECONDS, Job, JobQueue

Handler = Callable[[Job], dict[str, Any]]


def work(queue: JobQueue, handler: Handler, *, owner: str | None = None,
         lease_seconds: float = DEFAULT_LEASE_SECONDS, idle_exit: bool = True,
         stop: threading.Event | None = None, poll_seconds: float = 0.05) -> int:
    """Run jobs until the queue is drained (or ``stop``); returns how many this worker completed."""
    owner = owner or f"worker-{uuid4().hex[:8]}"
    completed = 0
    while stop is None or not stop.is_set():
        job = queue.claim(owner, lease_seconds=lease_seconds)
        if job is None:
            if idle_exit:
                return completed
            time.sleep(poll_seconds)
            continue
        alive = threading.Event()
        alive.set()

        def beat(current: Job = job) -> None:
            while alive.is_set():
                time.sleep(max(lease_seconds / 3, 0.01))
                if alive.is_set() and not queue.heartbeat(current, lease_seconds=lease_seconds):
                    return

        heart = threading.Thread(target=beat, daemon=True)
        heart.start()
        try:
            result = handler(job)
        except Exception as exc:
            alive.clear()
            queue.release(job, f"{type(exc).__name__}: {exc}")
            continue
        alive.clear()
        if queue.complete(job, result):
            completed += 1
    return completed


def run_pool(queue: JobQueue, handler: Handler, *, workers: int,
             lease_seconds: float = DEFAULT_LEASE_SECONDS) -> int:
    """``workers`` threads until the queue drains; returns total completions."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    totals: list[int] = []
    lock = threading.Lock()

    def one() -> None:
        count = work(queue, handler, lease_seconds=lease_seconds)
        with lock:
            totals.append(count)

    threads = [threading.Thread(target=one) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return sum(totals)


def worker_main(queue_path: str, handler_path: str, lease_seconds: float) -> None:
    """Process entry point: ``handler_path`` is ``module:function``."""
    import importlib

    module_name, _, function = handler_path.partition(":")
    handler = getattr(importlib.import_module(module_name), function)
    work(JobQueue(Path(queue_path)), handler, lease_seconds=lease_seconds)
