"""Job handlers for the queue crash tests (imported by worker processes)."""

from __future__ import annotations

import os
import random
import time
from pathlib import Path


def sleepy(job) -> dict:
    """Log every execution (to count re-executions), take a little time, return a result."""
    log = Path(os.environ["SENTINEL_QUEUE_TEST_LOG"])
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"{job.task_id}:{job.attempt}:{os.getpid()}\n")
    time.sleep(random.uniform(0.02, 0.15))
    return {"task_id": job.task_id, "attempt": job.attempt, "pid": os.getpid()}
