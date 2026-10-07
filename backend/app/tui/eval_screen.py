"""Recorded eval runs: pass rates with Wilson intervals, and the latest-vs-previous verdict.

The terminal twin of the desktop Eval page. Runs are global (not per Change),
so the screen opens from the dashboard without a selection. The comparison is
the backend's ``sentinel eval compare``: a drop counts only when the 95%
intervals separate or the paired bootstrap lies entirely below zero. Every
verdict pairs a text symbol with its colour, so it survives NO_COLOR.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from backend.app.cli.client import ApiClient, ApiConnectionError, ApiError


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _points(value: float) -> str:
    return f"{value * 100:+.1f} pts"


def verdict_label(comparison: dict[str, Any]) -> str:
    boot = comparison.get("bootstrap")
    detail = (f" (bootstrap {_points(boot['mean_difference'])}, 95% {_points(boot['low'])} to "
              f"{_points(boot['high'])})" if boot else "")
    if comparison.get("regression"):
        return f"[red]x REGRESSION{detail}[/red]"
    if boot and boot["low"] > 0:
        return f"[green]* improvement{detail}[/green]"
    return f"[white]= no significant regression{detail}[/white]"


def format_runs(runs: list[dict[str, Any]] | None, comparison: dict[str, Any] | None = None,
                error: str | None = None) -> str:
    if error:
        return f"[red]x {error}[/red]"
    if not runs:
        return "No evaluations recorded. Run `sentinel eval run ... --record` to add one."
    lines = ["[bold]Eval runs[/bold] (newest first)", ""]
    for run in runs:
        cost = ("unknown" if run.get("mean_cost_usd") is None
                else f"${run['mean_cost_usd']:.4f}")
        boundaries = ", ".join(f"{name}: {count}" for name, count
                               in sorted((run.get("hidden_boundaries") or {}).items())) or "unknown"
        state = "" if run.get("completed_at") else "  [yellow]~ in progress[/yellow]"
        lines.append(f"  {run['config_name']} · {run['agent']}"
                     f"{' · ' + run['model'] if run.get('model') else ''}{state}")
        lines.append(f"    {run['passes']}/{run['attempts']} passed  {_percent(run['rate'])} "
                     f"(95% {_percent(run['low'])} to {_percent(run['high'])})  "
                     f"errors {run['errors']}  mean cost {cost}")
        lines.append(f"    suite {run['suite']}  hidden tests under {boundaries}")
    if comparison is not None and len(runs) >= 2:
        lines += ["", f"[bold]Latest vs previous[/bold]  {verdict_label(comparison)}",
                  f"  {runs[1]['config_name']} -> {runs[0]['config_name']}: "
                  f"overall {_points(comparison['overall_delta'])}"]
        flipped = [task for task in comparison.get("tasks", []) if task.get("flip")]
        for task in flipped:
            symbol = "x" if task["flip"] == "regressed" else "*"
            lines.append(f"  {symbol} {task['task_id']} {task['flip']} "
                         f"({_percent(task['rate_a'])} -> {_percent(task['rate_b'])})")
    return "\n".join(lines)


class EvalScreen(Screen):
    """Recorded eval runs and the backend's regression verdict for the latest two."""

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    def __init__(self, api_url: str) -> None:
        super().__init__()
        self.client = ApiClient(api_url)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(Static("Loading evaluations...", id="eval_view"))
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self.run_worker(self._load, thread=True, exclusive=True)

    def _load(self) -> None:
        view = self.query_one("#eval_view", Static)
        runs: list[dict[str, Any]] | None = None
        comparison = None
        error = None
        try:
            runs = self.client.list_eval_runs().get("items", [])
            if len(runs) >= 2:
                comparison = self.client.compare_eval_runs(UUID(runs[1]["id"]),
                                                           UUID(runs[0]["id"]))
        except ApiConnectionError as exc:
            error = f"Could not reach the API: {exc}"
        except ApiError as exc:
            error = f"{exc.code}: {exc.message}"
        self.app.call_from_thread(view.update, format_runs(runs, comparison, error))
