"""Eval queue throughput, queue delay and crash recovery time.

    python bench/concurrency/run.py --jobs 64 --job-seconds 0.2 --out bench/concurrency/results

Jobs are simulated: each sleeps ``--job-seconds`` (standing in for an agent run),
so the numbers isolate the queue and worker pool (claims, leases, completion
writes) and how they scale with workers. They are not agent throughput.
Recovery: worker processes are SIGKILLed halfway, the queue is recovered, new
workers start, and the time to the first completion after the restart and to
the end of the run is measured.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.evals.queue import DONE, JobQueue  # noqa: E402
from backend.app.evals.stats import percentile  # noqa: E402
from backend.app.evals.workers import run_pool  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def throughput(jobs: int, job_seconds: float, workers: int, directory: Path) -> dict:
    queue = JobQueue(directory / f"q-{workers}.sqlite3")
    run = f"bench-{workers}"
    queue.enqueue(run, [f"t{i:04d}" for i in range(jobs)], k=1)
    started = time.time()
    run_pool(queue, lambda job: (time.sleep(job_seconds), {"task": job.task_id})[1],
             workers=workers, lease_seconds=30)
    elapsed = time.time() - started
    delays = [claimed - enqueued for enqueued, claimed, _done in queue.timings(run)]
    return {"workers": workers, "jobs": jobs, "seconds": elapsed,
            "jobs_per_hour": jobs / elapsed * 3600, "ideal_jobs_per_hour":
            min(workers, jobs) / job_seconds * 3600,
            "queue_delay_p50_s": statistics.median(delays),
            "queue_delay_p95_s": percentile(delays, 0.95)}


def recovery(jobs: int, directory: Path, workers: int = 4) -> dict:
    queue_path = directory / "recovery.sqlite3"
    queue = JobQueue(queue_path)
    queue.enqueue("recovery", [f"t{i:04d}" for i in range(jobs)], k=1)
    env = {**os.environ, "SENTINEL_QUEUE_TEST_LOG": str(directory / "log"), "PYTHONPATH": str(REPO)}
    code = ("from backend.app.evals.workers import worker_main; "
            f"worker_main({str(queue_path)!r}, 'backend.tests.evals.queue_handlers:sleepy', 30.0)")

    def spawn():
        return [subprocess.Popen([sys.executable, "-c", code], cwd=REPO, env=env)
                for _ in range(workers)]

    procs = spawn()
    while queue.counts("recovery")[DONE] < jobs // 2:
        time.sleep(0.02)
    for proc in procs:
        proc.kill()
        proc.wait()
    done_at_kill = queue.counts("recovery")[DONE]
    restart = time.time()
    recovered = queue.recover()
    procs = spawn()
    first = None
    while queue.counts("recovery")[DONE] < jobs:
        if first is None and queue.counts("recovery")[DONE] > done_at_kill:
            first = time.time() - restart
        time.sleep(0.01)
    total = time.time() - restart
    for proc in procs:
        proc.wait()
    results = queue.results("recovery")
    return {"jobs": jobs, "workers": workers, "done_at_kill": done_at_kill,
            "leases_recovered": recovered, "first_completion_after_restart_s": first,
            "restart_to_finish_s": total, "jobs_lost": jobs - len(results),
            "duplicate_completions": len(results) - len({r["task_id"] for r in results})}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=64)
    parser.add_argument("--job-seconds", type=float, default=0.2)
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("results"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        rows = [throughput(args.jobs, args.job_seconds, n, Path(directory)) for n in (1, 4, 8, 16)]
        recovered = recovery(args.jobs, Path(directory))
    result = {"command": " ".join(sys.argv), "machine": {
        "platform": platform.platform(), "python": platform.python_version(),
        "cpus": os.cpu_count()}, "job_seconds": args.job_seconds, "throughput": rows,
        "recovery": recovered}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
