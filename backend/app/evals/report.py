"""Eval results files, summaries, run comparison and the static HTML report.

A results file (``sentinel-eval-run/1``) is the whole run: config, suite, k, and
every attempt. Everything else is derived from it, so ``compare`` works offline
on two files (a CI gate needs no server):

* :func:`summarize`: per task passes/attempts with a Wilson 95% interval, the
  overall rate with its interval, mean cost over the attempts that reported one
  (and how many did not), and p50/p95 wall time. ERROR attempts count as
  attempts that did not pass; they are also listed separately.
* :func:`compare`: per-task and overall deltas, the tasks that flipped (majority
  pass -> majority fail or back), a paired bootstrap over shared tasks, and the
  regression verdict (`stats.is_regression`).
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from backend.app.evals.runner import EvalRun
from backend.app.evals.stats import (
    BootstrapResult,
    is_regression,
    paired_bootstrap,
    percentile,
    wilson_interval,
)

SCHEMA = "sentinel-eval-run/1"


def run_to_document(run: EvalRun) -> dict[str, Any]:
    return {"schema": SCHEMA, "id": run.id, "suite": run.suite, "config": run.config.to_payload(),
            "k": run.k, "started_at": run.started_at, "completed_at": run.completed_at,
            "results": [result.to_payload() for result in run.results]}


def load_document(path: Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} results file")
    if not isinstance(document.get("results"), list):
        raise ValueError(f"{path} has no results list")
    return document


def write_document(document: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class TaskSummary:
    task_id: str
    passes: int
    attempts: int
    errors: int
    rate: float
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    config: str
    tasks: list[TaskSummary]
    passes: int
    attempts: int
    errors: int
    rate: float
    low: float
    high: float
    mean_cost_usd: float | None
    cost_unknown: int
    wall_p50_seconds: float | None
    wall_p95_seconds: float | None
    hidden_boundaries: dict[str, int] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def summarize(document: Mapping[str, Any]) -> RunSummary:
    by_task: dict[str, list[Mapping[str, Any]]] = {}
    for result in document["results"]:
        by_task.setdefault(result["task_id"], []).append(result)
    tasks = []
    for task_id in sorted(by_task):
        results = by_task[task_id]
        passes = sum(item["status"] == "PASSED" for item in results)
        interval = wilson_interval(passes, len(results))
        tasks.append(TaskSummary(task_id, passes, len(results),
                                 sum(item["status"] == "ERROR" for item in results),
                                 passes / len(results), interval.low, interval.high))
    results = document["results"]
    passes = sum(item["status"] == "PASSED" for item in results)
    interval = wilson_interval(passes, len(results))
    costs = [item["agent"]["cost_usd"] for item in results
             if item.get("agent") and item["agent"].get("cost_usd") is not None]
    walls = [float(item["wall_seconds"]) for item in results]
    boundaries: dict[str, int] = {}
    for item in results:
        boundary = (item.get("hidden") or {}).get("boundary") or "NONE"
        boundaries[boundary] = boundaries.get(boundary, 0) + 1
    return RunSummary(
        run_id=str(document["id"]), config=str(document["config"].get("name")), tasks=tasks,
        passes=passes, attempts=len(results),
        errors=sum(item["status"] == "ERROR" for item in results),
        rate=(passes / len(results)) if results else 0.0, low=interval.low, high=interval.high,
        mean_cost_usd=(sum(costs) / len(costs)) if costs else None,
        cost_unknown=len(results) - len(costs),
        wall_p50_seconds=percentile(walls, 0.5) if walls else None,
        wall_p95_seconds=percentile(walls, 0.95) if walls else None,
        hidden_boundaries=dict(sorted(boundaries.items())),
    )


@dataclass(frozen=True, slots=True)
class Comparison:
    a: RunSummary
    b: RunSummary
    task_deltas: dict[str, float]
    regressed: list[str]  # majority pass in A, majority fail in B
    improved: list[str]
    only_in_a: list[str]
    only_in_b: list[str]
    bootstrap: BootstrapResult | None
    regression: bool

    def to_payload(self) -> dict[str, Any]:
        return {"a": self.a.to_payload(), "b": self.b.to_payload(),
                "task_deltas": self.task_deltas, "regressed": self.regressed,
                "improved": self.improved, "only_in_a": self.only_in_a,
                "only_in_b": self.only_in_b,
                "bootstrap": None if self.bootstrap is None else {
                    "mean_difference": self.bootstrap.mean_difference,
                    "low": self.bootstrap.interval.low, "high": self.bootstrap.interval.high,
                    "resamples": self.bootstrap.resamples, "tasks": self.bootstrap.tasks},
                "overall_delta": self.b.rate - self.a.rate, "regression": self.regression}


def compare(document_a: Mapping[str, Any], document_b: Mapping[str, Any], *,
            resamples: int = 10_000, seed: int | None = None) -> Comparison:
    a, b = summarize(document_a), summarize(document_b)
    rates_a = {task.task_id: task.rate for task in a.tasks}
    rates_b = {task.task_id: task.rate for task in b.tasks}
    shared = sorted(set(rates_a) & set(rates_b))
    deltas = {task: rates_b[task] - rates_a[task] for task in shared}
    regressed = [task for task in shared if rates_a[task] >= 0.5 > rates_b[task]]
    improved = [task for task in shared if rates_b[task] >= 0.5 > rates_a[task]]
    kwargs = {"resamples": resamples} if seed is None else {"resamples": resamples, "seed": seed}
    bootstrap = paired_bootstrap(rates_a, rates_b, **kwargs) if shared else None
    passes_a = sum(task.passes for task in a.tasks if task.task_id in shared)
    attempts_a = sum(task.attempts for task in a.tasks if task.task_id in shared)
    passes_b = sum(task.passes for task in b.tasks if task.task_id in shared)
    attempts_b = sum(task.attempts for task in b.tasks if task.task_id in shared)
    regression = bool(shared) and is_regression((passes_a, attempts_a), (passes_b, attempts_b),
                                                bootstrap)
    return Comparison(a, b, deltas, regressed, improved, sorted(set(rates_a) - set(rates_b)),
                      sorted(set(rates_b) - set(rates_a)), bootstrap, regression)


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_html(document: Mapping[str, Any]) -> str:
    """A self-contained static page (no scripts, no external assets)."""
    summary = summarize(document)
    config = document["config"]
    rows = "\n".join(
        f"<tr><td>{html.escape(task.task_id)}</td><td>{task.passes}/{task.attempts}</td>"
        f"<td>{_percent(task.rate)}</td><td>{_percent(task.low)} to {_percent(task.high)}</td>"
        f"<td>{task.errors}</td></tr>" for task in summary.tasks)
    cost = ("UNKNOWN" if summary.mean_cost_usd is None
            else f"${summary.mean_cost_usd:.4f} (unknown for {summary.cost_unknown})")
    wall = ("UNKNOWN" if summary.wall_p50_seconds is None else
            f"{summary.wall_p50_seconds:.1f} s / {summary.wall_p95_seconds:.1f} s")
    boundaries = ", ".join(f"{html.escape(name)}: {count}"
                           for name, count in summary.hidden_boundaries.items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentinel eval {html.escape(summary.config)}</title>
<style>
:root {{ --fg:#1b1f24; --bg:#ffffff; --muted:#57606a; --line:#d0d7de; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg:#e6edf3; --bg:#0d1117; --muted:#8d96a0; --line:#30363d; }} }}
body {{ font: 15px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg);
       max-width: 960px; margin: 2rem auto; padding: 0 16px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; }}
.muted {{ color: var(--muted); }}
.headline {{ font-size: 1.6rem; font-weight: 600; }}
</style></head><body>
<h1>Eval: {html.escape(summary.config)}</h1>
<p class="muted">Run {html.escape(summary.run_id)} · suite {html.escape(str(document.get("suite")))}
 · agent {html.escape(str(config.get("agent")))} · k = {int(document.get("k", 0))}
 · {html.escape(str(document.get("started_at")))}</p>
<p class="headline">{summary.passes}/{summary.attempts} passed · {_percent(summary.rate)}
 (95% Wilson {_percent(summary.low)} to {_percent(summary.high)})</p>
<p>Errors: {summary.errors} · mean cost: {cost} · wall time p50/p95: {wall}</p>
<p class="muted">Hidden tests ran under: {boundaries}</p>
<table><thead><tr><th>Task</th><th>Passed</th><th>Rate</th><th>95% interval</th><th>Errors</th></tr>
</thead><tbody>
{rows}
</tbody></table>
</body></html>
"""
