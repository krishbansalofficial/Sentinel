"""The eval job queue: leases, fencing, recovery after SIGKILL, and a worker chaos run."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path


from backend.app.evals.queue import DONE, FAILED, QUEUED, RUNNING, JobQueue
from backend.app.evals.workers import run_pool

REPO = Path(__file__).resolve().parents[3]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_enqueue_is_idempotent_and_counts(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "q.sqlite3")
    assert queue.enqueue("run", ["a", "b"], k=3) == 6
    assert queue.enqueue("run", ["a", "b"], k=3) == 0
    assert queue.counts("run") == {QUEUED: 6, RUNNING: 0, DONE: 0, FAILED: 0}


def test_a_stale_worker_cannot_complete_a_reclaimed_job(tmp_path: Path) -> None:
    clock = Clock()
    queue = JobQueue(tmp_path / "q.sqlite3", clock=clock)
    queue.enqueue("run", ["a"], k=1)
    first = queue.claim("worker-1", lease_seconds=10)
    assert first is not None and queue.claim("worker-2", lease_seconds=10) is None
    clock.now += 11  # worker-1 stalls past its lease
    second = queue.claim("worker-2", lease_seconds=10)
    assert second is not None and second.id == first.id and second.claims == 2
    assert not queue.heartbeat(first)
    assert queue.complete(second, {"by": "worker-2"})
    assert not queue.complete(first, {"by": "worker-1"})  # fenced off
    assert queue.results("run") == [{"by": "worker-2"}]


def test_heartbeats_keep_a_slow_job(tmp_path: Path) -> None:
    clock = Clock()
    queue = JobQueue(tmp_path / "q.sqlite3", clock=clock)
    queue.enqueue("run", ["a"], k=1)
    job = queue.claim("w", lease_seconds=10)
    for _ in range(5):
        clock.now += 8
        assert queue.heartbeat(job, lease_seconds=10)
    assert queue.claim("other", lease_seconds=10) is None
    assert queue.complete(job, {"ok": True})


def test_a_job_that_keeps_dying_becomes_failed(tmp_path: Path) -> None:
    clock = Clock()
    queue = JobQueue(tmp_path / "q.sqlite3", clock=clock, max_claims=3)
    queue.enqueue("run", ["a"], k=1)
    for _ in range(3):
        assert queue.claim("w", lease_seconds=1) is not None
        clock.now += 2
    assert queue.claim("w", lease_seconds=1) is None
    assert queue.counts("run")[FAILED] == 1


def test_handler_errors_release_the_job(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "q.sqlite3", max_claims=2)
    queue.enqueue("run", ["flaky", "broken"], k=1)
    calls: Counter = Counter()

    def handler(job):
        calls[job.task_id] += 1
        if job.task_id == "broken" or calls[job.task_id] == 1:
            raise RuntimeError("boom")
        return {"task": job.task_id}

    run_pool(queue, handler, workers=1, lease_seconds=5)
    assert queue.counts("run") == {QUEUED: 0, RUNNING: 0, DONE: 1, FAILED: 1}
    assert calls == {"flaky": 2, "broken": 2}


def test_concurrent_claims_never_hand_out_a_job_twice(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "q.sqlite3")
    queue.enqueue("run", [f"t{i:03d}" for i in range(200)], k=1)
    claimed: list[str] = []
    lock = threading.Lock()

    def grab() -> None:
        while (job := queue.claim(f"w{threading.get_ident()}", lease_seconds=60)) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == 200 and len(set(claimed)) == 200


def test_a_thread_pool_drains_100_jobs_exactly_once(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "q.sqlite3")
    queue.enqueue("run", [f"t{i:03d}" for i in range(50)], k=2)
    completed = run_pool(queue, lambda job: {"task": job.task_id, "attempt": job.attempt},
                         workers=8, lease_seconds=5)
    assert completed == 100
    results = queue.results("run")
    assert len({(r["task"], r["attempt"]) for r in results}) == 100


def _spawn_worker(queue_path: Path, log: Path, lease: float) -> subprocess.Popen:
    code = ("from backend.app.evals.workers import worker_main; "
            f"worker_main({str(queue_path)!r}, 'backend.tests.evals.queue_handlers:sleepy', {lease})")
    env = {**os.environ, "SENTINEL_QUEUE_TEST_LOG": str(log), "PYTHONPATH": str(REPO)}
    return subprocess.Popen([sys.executable, "-c", code], cwd=REPO, env=env)


def _assert_exactly_once(queue: JobQueue, run: str, expected: int) -> None:
    assert queue.counts(run) == {QUEUED: 0, RUNNING: 0, DONE: expected, FAILED: 0}
    keys = [(r["task_id"], r["attempt"]) for r in queue.results(run)]
    assert len(keys) == expected and len(set(keys)) == expected


def test_sigkill_mid_run_then_restart_finishes_every_job_once(tmp_path: Path) -> None:
    queue_path, log = tmp_path / "q.sqlite3", tmp_path / "executions.log"
    queue = JobQueue(queue_path)
    queue.enqueue("run", [f"t{i:02d}" for i in range(30)], k=1)
    workers = [_spawn_worker(queue_path, log, lease=30.0) for _ in range(3)]
    deadline = time.monotonic() + 30
    while queue.counts("run")[DONE] < 5 and time.monotonic() < deadline:
        time.sleep(0.05)
    for worker in workers:
        worker.kill()  # SIGKILL on POSIX, TerminateProcess on Windows: no cleanup runs
        worker.wait(10)
    interrupted = queue.counts("run")[RUNNING]
    assert queue.recover() == interrupted  # restart: the dead owners' leases are released
    restarted = [_spawn_worker(queue_path, log, lease=30.0) for _ in range(3)]
    for worker in restarted:
        assert worker.wait(120) == 0
    _assert_exactly_once(queue, "run", 30)
    executions = Counter(line.rsplit(":", 1)[0] for line in log.read_text().splitlines())
    assert set(executions) == {f"t{i:02d}:1" for i in range(30)}
    assert sum(count - 1 for count in executions.values()) <= interrupted  # re-runs: only those cut off


def test_chaos_random_worker_kills_during_a_100_job_run(tmp_path: Path) -> None:
    queue_path, log = tmp_path / "q.sqlite3", tmp_path / "executions.log"
    queue = JobQueue(queue_path)
    queue.enqueue("chaos", [f"t{i:03d}" for i in range(100)], k=1)
    rng = random.Random(20261004)
    workers = [_spawn_worker(queue_path, log, lease=1.0) for _ in range(6)]
    kills = 0
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline and queue.counts("chaos")[DONE] < 100:
        time.sleep(rng.uniform(0.1, 0.4))
        live = [w for w in workers if w.poll() is None]
        if live and kills < 12 and rng.random() < 0.6:
            victim = rng.choice(live)
            victim.kill()
            victim.wait(10)
            kills += 1
        workers = [w for w in workers if w.poll() is None]
        while len(workers) < 6 and queue.counts("chaos")[DONE] < 100:
            workers.append(_spawn_worker(queue_path, log, lease=1.0))
    for worker in workers:
        worker.wait(60)
    assert kills >= 5
    _assert_exactly_once(queue, "chaos", 100)  # nothing lost, nothing completed twice
