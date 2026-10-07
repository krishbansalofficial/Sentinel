# Eval queue throughput, queue delay and crash recovery

Measures the SQLite (WAL) job queue and worker pool from `backend/app/evals/queue.py` and
`workers.py`. **Jobs are simulated**: each sleeps 0.2 s in place of an agent run, so these
numbers isolate the queue's own cost (claims, leases, heartbeats, fenced completions) and how
it scales. They are not agent throughput; a real run is bounded by the agent.

## Result (2026-10-07, 64 jobs of 0.2 s)

| Workers | Jobs/hour | Ideal jobs/hour | Efficiency | Queue delay p50 | p95 |
| --- | --- | --- | --- | --- | --- |
| 1 | 15,366 | 18,000 | 85% | 7.42 s | 14.03 s |
| 4 | 61,809 | 72,000 | 86% | 1.77 s | 3.41 s |
| 8 | 113,384 | 144,000 | 79% | 0.91 s | 1.71 s |
| 16 | 167,332 | 288,000 | 58% | 0.56 s | 1.10 s |

Queue delay is enqueue to first claim, so it mostly reflects backlog (all 64 jobs are enqueued
at once). Efficiency falls at 16 workers because 64 short jobs leave the tail of the run with
idle workers and the single SQLite writer serializes claims; longer, agent-sized jobs would
amortize both.

**Recovery** (4 worker processes, SIGKILLed when 32 of 64 jobs were done): 4 leases recovered,
first completion 0.14 s after restart, the remaining 32 jobs finished 1.12 s after restart,
**0 jobs lost, 0 duplicate completions** (fencing tokens; a cut-off job may *execute* twice,
it is *completed* once).

## Exact command and environment

```bash
python bench/concurrency/run.py --jobs 64 --job-seconds 0.2 --out /tmp/cb
```

Docker `python:3.12-slim` on Windows 11 + WSL2 (kernel 5.15.167.4), Intel Core i9-14900HX,
32 logical CPUs. Raw output: [`results/summary.json`](results/summary.json).
