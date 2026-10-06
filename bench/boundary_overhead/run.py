"""Launch overhead of the Linux sandbox: request -> first byte from the agent.

Usage (Linux, bwrap installed, run inside a delegated cgroup v2 subtree):

    SENTINEL_CGROUP_PARENT=/sys/fs/cgroup/<delegated> python bench/boundary_overhead/run.py \
        --launches 200 --out bench/boundary_overhead/results

For each launch the clock starts just before the launch call and stops when the
agent's first stdout byte arrives, so the sandboxed number includes cgroup
creation, bwrap namespace setup, the seccomp filter, the /proc verification and
the release. The baseline is a plain ``subprocess.Popen`` of the same program.
``setup`` is the sandbox-only part: request until the verified process is
released (the agent has not started yet). End-to-end overhead can be smaller
than setup because the agent program itself may start faster inside the
sandbox (cleared environment); both are reported, never one in place of the other.
Raw per-launch timings are written as CSV next to a JSON summary.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.execution.cgroups import CgroupHierarchy, RunCgroup, RunLimits  # noqa: E402
from backend.app.execution.linux_sandbox import SandboxSpec, spawn_linux_sandbox  # noqa: E402

PROGRAM = "import sys; sys.stdout.write('x'); sys.stdout.flush()"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def baseline(python: str) -> float:
    started = time.perf_counter()
    process = subprocess.Popen([python, "-c", PROGRAM], stdout=subprocess.PIPE)
    process.stdout.read(1)
    elapsed = time.perf_counter() - started
    process.wait()
    process.stdout.close()
    return elapsed


def sandboxed(python: str, hierarchy, workspace: Path, controllers: bool) -> tuple[float, float]:
    started = time.perf_counter()
    run_id = uuid4()
    if controllers:
        cgroup = hierarchy.create_run(run_id, RunLimits())
    else:  # a dev box without delegated controllers: membership/freeze/kill only
        path = hierarchy.parent / f"sentinel-run-{run_id.hex}"
        path.mkdir()
        cgroup = RunCgroup(path, hierarchy.root)
    spec = SandboxSpec(argv=(python, "-c", PROGRAM), cwd=workspace,
                       env={"PATH": "/usr/local/bin:/usr/bin:/bin"}, writable=(workspace,))
    process = spawn_linux_sandbox(spec, cgroup, redact=lambda text: text)
    released = time.perf_counter() - started
    process.stdout.read(1)
    elapsed = time.perf_counter() - started
    process.wait()
    process.close()
    return elapsed, released


def summary(values: list[float]) -> dict[str, float]:
    return {"n": len(values), "median_ms": statistics.median(values) * 1000,
            "p95_ms": _percentile(values, 0.95) * 1000, "min_ms": min(values) * 1000,
            "max_ms": max(values) * 1000}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launches", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("results"))
    parser.add_argument("--no-controllers", action="store_true",
                        help="the delegated subtree has no pids/memory controllers (dev box)")
    args = parser.parse_args()
    python = os.path.realpath(sys.executable)
    hierarchy = CgroupHierarchy.from_environment()
    if not args.no_controllers:
        hierarchy.prepare()
    args.out.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, int, float]] = []
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        for _ in range(args.warmup):
            baseline(python)
            sandboxed(python, hierarchy, workspace, not args.no_controllers)
        for index in range(args.launches):  # interleaved so drift hits both equally
            rows.append(("baseline", index, baseline(python)))
            total, setup = sandboxed(python, hierarchy, workspace, not args.no_controllers)
            rows.append(("sandbox", index, total))
            rows.append(("sandbox_setup", index, setup))
    with (args.out / "launches.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["mode", "index", "seconds"])
        writer.writerows((mode, index, f"{value:.6f}") for mode, index, value in rows)
    base = [value for mode, _, value in rows if mode == "baseline"]
    box = [value for mode, _, value in rows if mode == "sandbox"]
    setup = [value for mode, _, value in rows if mode == "sandbox_setup"]
    result = {
        "command": " ".join(sys.argv),
        "machine": {"platform": platform.platform(), "machine": platform.machine(),
                    "python": platform.python_version(), "cpus": os.cpu_count(),
                    "cpu_model": _cpu_model()},
        "controllers_delegated": not args.no_controllers,
        "baseline": summary(base), "sandbox": summary(box), "sandbox_setup": summary(setup),
        "overhead_median_ms": (statistics.median(box) - statistics.median(base)) * 1000,
        "overhead_p95_ms": (_percentile(box, 0.95) - _percentile(base, 0.95)) * 1000,
    }
    (args.out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
