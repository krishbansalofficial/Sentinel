# Linux sandbox launch overhead

What it measures: for 200 launches each, the time from a launch request until the agent's
first stdout byte, with the verified Linux sandbox (`spawn_linux_sandbox`: run cgroup,
bubblewrap namespaces, seccomp filter, `/proc` verification, release) and without it (a plain
`subprocess.Popen`). Launches alternate so machine drift hits both modes equally. A separate
`sandbox_setup` series times the sandbox-only part: request until the verified process is
released, before the agent has started.

## Result (2026-10-04)

| Series | n | median | p95 |
| --- | --- | --- | --- |
| Baseline (no sandbox), end to end | 200 | 14.15 ms | 16.08 ms |
| Sandbox, end to end | 200 | 14.39 ms | 17.17 ms |
| **Sandbox setup (request to verified release)** | 200 | **7.37 ms** | **8.15 ms** |

Read it as: establishing and verifying the boundary costs about 7.4 ms (p95 8.1 ms) per
launch. The end-to-end difference is only about 0.24 ms (p95 1.1 ms), because the agent
program itself started about 7 ms faster inside the sandbox. The likely cause is the cleared
environment and the absent user site-packages (this image has an editable `pip --user`
install whose `.pth` processing slows every host Python start). Quote the setup number as
the boundary's cost; the end-to-end number depends on the agent.

## Exact command and environment

```bash
# privileged Docker container on the host below; /sys/fs/cgroup remounted as cgroup2,
# a delegated subtree owned by the test user, the shell moved into a leaf of it
SENTINEL_CGROUP_PARENT=/sys/fs/cgroup/sentinel-test-<pid> \
  python bench/boundary_overhead/run.py --launches 200 --no-controllers --out /tmp/bench
```

* Host: Intel Core i9-14900HX, 32 logical CPUs, Windows 11 + WSL2 kernel
  5.15.167.4-microsoft-standard-WSL2, Docker Desktop.
* Container: `python:3.12-slim-bookworm` (Python 3.12.15), bubblewrap 0.8.0.
* `--no-controllers`: this WSL2 kernel binds the pids/memory/cpu controllers to the cgroup
  v1 hierarchy, so the run cgroups here have no limits to write. That skips three small
  file writes per launch; everything else (cgroup creation, membership, verification) is
  included. A host with delegated cgroup v2 controllers (CI's `linux-sandbox` job) runs
  without the flag.

## Raw output

* [`results/summary.json`](results/summary.json): the summary above, with the command line.
* [`results/launches.csv`](results/launches.csv): every launch (`mode,index,seconds`).
