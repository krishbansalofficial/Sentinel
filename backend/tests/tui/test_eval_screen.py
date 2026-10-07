"""The TUI Eval screen: formatting, and a real load against a live API server."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.app.tui.eval_screen import EvalScreen, format_runs, verdict_label
from backend.tests.tui.test_pilot_real_worker_flows import _wait_until, live_change  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _run(name: str, passes: int, attempts: int, **extra) -> dict:
    rate = passes / attempts
    return {"id": str(uuid4()), "config_name": name, "agent": "mock", "model": None,
            "suite": "evals/tasks", "passes": passes, "attempts": attempts, "errors": 0,
            "rate": rate, "low": max(0.0, rate - 0.1), "high": min(1.0, rate + 0.1),
            "mean_cost_usd": None, "hidden_boundaries": {"LINUX_SANDBOX": attempts},
            "completed_at": "2026-10-06T12:00:00+00:00", **extra}


def test_empty_and_error_states_are_honest() -> None:
    assert "No evaluations recorded" in format_runs([])
    assert "--record" in format_runs(None)
    assert format_runs(None, error="Could not reach the API") == "[red]x Could not reach the API[/red]"


def test_runs_show_rate_interval_unknown_cost_and_boundaries() -> None:
    text = format_runs([_run("baseline", 77, 90, completed_at=None)])
    assert "77/90 passed  85.6% (95% 75.6% to 95.6%)" in text
    assert "mean cost unknown" in text and "LINUX_SANDBOX: 90" in text
    assert "~ in progress" in text


def test_verdicts_carry_a_text_symbol_not_just_colour() -> None:
    boot = {"mean_difference": -0.33, "low": -0.46, "high": -0.21, "resamples": 10_000, "tasks": 30}
    assert verdict_label({"regression": True, "bootstrap": boot}).startswith("[red]x REGRESSION")
    gain = verdict_label({"regression": False,
                          "bootstrap": {**boot, "mean_difference": 0.2, "low": 0.1, "high": 0.3}})
    assert "* improvement" in gain
    assert "= no significant regression" in verdict_label({"regression": False, "bootstrap": None})


def test_latest_vs_previous_lists_flipped_tasks() -> None:
    runs = [_run("broken", 0, 90), _run("baseline", 77, 90)]
    comparison = {"regression": True, "overall_delta": -0.856, "bootstrap": None,
                  "tasks": [{"task_id": "op-add", "rate_a": 1.0, "rate_b": 0.0, "flip": "regressed"},
                            {"task_id": "clamp-0", "rate_a": 1.0, "rate_b": 1.0, "flip": None}]}
    text = format_runs(runs, comparison)
    assert "baseline -> broken: overall -85.6 pts" in text
    assert "x op-add regressed (100.0% -> 0.0%)" in text and "clamp-0" not in text


def _document(name: str, outcome: str) -> dict:
    return {"schema": "sentinel-eval-run/1", "id": str(uuid4()), "suite": "evals/tasks",
            "config": {"name": name, "agent": "mock"}, "k": 3,
            "started_at": f"2026-10-06T1{0 if outcome == 'PASSED' else 1}:00:00+00:00",
            "completed_at": "2026-10-06T12:00:00+00:00",
            "results": [{"task_id": f"t{task:02d}", "attempt": attempt, "status": outcome,
                         "wall_seconds": 1.0, "agent": None,
                         "hidden": {"boundary": "LINUX_SANDBOX"}}
                        for task in range(10) for attempt in (1, 2, 3)]}


@pytest.mark.anyio
async def test_eval_screen_real_load_shows_the_backend_verdict(live_change) -> None:
    from backend.app.cli.client import ApiClient
    from backend.app.tui.app import ChangeDashboard
    from textual.widgets import Static

    api_url, _change_id, actor_id, _human_id = live_change
    client = ApiClient(api_url)
    client.record_eval_run(_document("baseline", "PASSED"))
    client.record_eval_run(_document("broken", "FAILED"))
    app = ChangeDashboard(api_url=api_url, actor_id=actor_id)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, EvalScreen)
        view = app.screen.query_one("#eval_view", Static)
        assert await _wait_until(pilot, lambda: "Latest vs previous" in str(view.render()))
        text = str(view.render())
        assert "x REGRESSION" in text and "baseline -> broken" in text
        assert "x t00 regressed" in text
