"""Backtest `sentinel eval compare` with mock data: does it catch regressions, and only those?

Part A (real pipeline): the 30-task seed suite, k attempts per task, the mock agent, real
fixture repositories and real hidden tests. Four configs:

* ``baseline-a`` / ``baseline-b``: the same config (skill 0.85), different seeds. Comparing
  them must not flag a regression.
* ``degraded``: skill 0.50, an injected model regression. Must flag.
* ``broken-prompt``: the template drops ``{prompt}`` (an injected prompt regression). Must flag.

Hidden tests run in the verified Linux sandbox with ``--confined`` (needs bubblewrap and a
delegated cgroup v2 subtree in ``SENTINEL_CGROUP_PARENT``; ``--no-controllers`` accepts one
without pids/memory, as on hybrid-cgroup dev boxes); otherwise as host children, labelled
UNCONFINED in every result.

Part B (Monte Carlo): synthetic result documents with per-attempt Bernoulli outcomes (the
mock agent's model), compared with ``report.compare`` exactly as the CLI does. For each true
pass-rate drop it reports how often a regression is flagged: the drop-0 row is the false
positive rate, the others are detection power.

    python bench/regression_detection/run.py --out bench/regression_detection/results
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.evals.agents import MockAgentDriver  # noqa: E402
from backend.app.evals.report import SCHEMA, compare, render_html, run_to_document, summarize  # noqa: E402
from backend.app.evals.report import write_document  # noqa: E402
from backend.app.evals.runner import AgentConfig, EvalRunner  # noqa: E402
from backend.app.evals.suite import load_suite  # noqa: E402

CONFIGS = (
    ("baseline-a", 0.85, 1, "{prompt}"),
    ("baseline-b", 0.85, 2, "{prompt}"),
    ("degraded", 0.50, 3, "{prompt}"),
    ("broken-prompt", 0.85, 4, "{title}"),
)
PAIRS = (
    ("baseline-a", "baseline-b", False),
    ("baseline-a", "degraded", True),
    ("baseline-a", "broken-prompt", True),
)


def _hidden_runner(confined: bool, no_controllers: bool):
    if not confined:
        from backend.app.evals.hidden import UnconfinedHiddenTestRunner

        return UnconfinedHiddenTestRunner()
    from backend.app.evals.hidden import SandboxHiddenTestRunner
    from backend.app.execution.cgroups import CgroupHierarchy, RunCgroup

    if not no_controllers:
        return SandboxHiddenTestRunner(CgroupHierarchy.from_environment)

    class Membership(CgroupHierarchy):  # freeze/kill/membership only, no limits
        def prepare(self):
            return ()

        def create_run(self, run_id, limits):
            path = self.parent / f"sentinel-run-{run_id.hex}"
            path.mkdir()
            return RunCgroup(path, self.root)

    return SandboxHiddenTestRunner(lambda: Membership(Path(os.environ["SENTINEL_CGROUP_PARENT"])))


def part_a(out: Path, *, k: int, confined: bool, no_controllers: bool) -> dict:
    tasks = load_suite(ROOT / "evals" / "tasks")
    hidden = _hidden_runner(confined, no_controllers)
    documents, timings = {}, {}
    for name, skill, seed, template in CONFIGS:
        runner = EvalRunner(MockAgentDriver(skill=skill, seed=seed), hidden)
        started = time.monotonic()
        run = runner.run(tasks, config=AgentConfig(name, prompt_template=template), k=k,
                         suite="evals/tasks")
        timings[name] = round(time.monotonic() - started, 2)
        document = run_to_document(run)
        write_document(document, out / f"{name}.json")
        (out / f"{name}.html").write_text(render_html(document), encoding="utf-8")
        documents[name] = document
    summaries = {}
    for name, document in documents.items():
        summary = summarize(document)
        summaries[name] = {"passes": summary.passes, "attempts": summary.attempts,
                           "errors": summary.errors, "rate": round(summary.rate, 4),
                           "wilson_95": [round(summary.low, 4), round(summary.high, 4)],
                           "hidden_boundaries": summary.hidden_boundaries,
                           "wall_p50_seconds": summary.wall_p50_seconds,
                           "seconds": timings[name]}
    comparisons = []
    for a, b, expected in PAIRS:
        comparison = compare(documents[a], documents[b])
        boot = comparison.bootstrap
        comparisons.append({
            "a": a, "b": b, "expected_regression": expected,
            "flagged": comparison.regression, "correct": comparison.regression == expected,
            "overall_delta": round(comparison.b.rate - comparison.a.rate, 4),
            "bootstrap_mean": round(boot.mean_difference, 4),
            "bootstrap_95": [round(boot.interval.low, 4), round(boot.interval.high, 4)],
            "regressed_tasks": len(comparison.regressed), "improved_tasks": len(comparison.improved)})
    return {"tasks": len(tasks), "k": k, "confined": confined, "runs": summaries,
            "comparisons": comparisons}


def _synthetic(run_id: str, rate: float, tasks: int, k: int, generator: random.Random) -> dict:
    results = [{"run_id": run_id, "task_id": f"t{task:02d}", "attempt": attempt,
                "status": "PASSED" if generator.random() < rate else "FAILED",
                "hidden_tests_absent": True, "wall_seconds": 1.0, "agent": None,
                "hidden": {"boundary": "SYNTHETIC"}}
               for task in range(tasks) for attempt in range(1, k + 1)]
    return {"schema": SCHEMA, "id": run_id, "suite": "synthetic", "k": k,
            "config": {"name": f"p={rate}"}, "results": results}


def part_b(*, trials: int, tasks: int, k: int, base: float, drops: list[float],
           resamples: int, seed: int) -> list[dict]:
    generator = random.Random(seed)
    rows = []
    for drop in drops:
        flagged = 0
        for trial in range(trials):
            a = _synthetic(str(uuid4()), base, tasks, k, generator)
            b = _synthetic(str(uuid4()), base - drop, tasks, k, generator)
            flagged += compare(a, b, resamples=resamples, seed=seed + trial).regression
        rows.append({"base_rate": base, "drop": drop, "trials": trials, "flagged": flagged,
                     "flag_rate": round(flagged / trials, 4)})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--confined", action="store_true")
    parser.add_argument("--no-controllers", action="store_true")
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--resamples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261006)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "command": " ".join(["python", *sys.argv]),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "cpus": os.cpu_count()},
        "part_a": part_a(args.out, k=args.k, confined=args.confined,
                         no_controllers=args.no_controllers),
        "part_b": part_b(trials=args.trials, tasks=30, k=args.k, base=0.85,
                         drops=[0.0, 0.05, 0.10, 0.15, 0.20, 0.30], resamples=args.resamples,
                         seed=args.seed),
    }
    summary["seconds"] = round(time.monotonic() - started, 1)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
