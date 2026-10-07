"""The seeded suite is sound, and ``sentinel eval`` runs, reports and gates end to end.

Hidden tests here use the explicit unconfined runner (labelled UNCONFINED); the
confined runners are exercised in tests/execution/linux and on Windows CI.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backend.app.cli.eval_commands import EXIT_REGRESSION
from backend.app.cli.main import app
from backend.app.evals.hidden import UnconfinedHiddenTestRunner
from backend.app.evals.report import load_document, render_html, summarize
from backend.app.evals.runner import hidden_tests_absent, materialize_fixture, prepare_hidden_tree
from backend.app.evals.suite import load_suite

SUITE = Path(__file__).resolve().parents[3] / "evals" / "tasks"


def test_the_seed_suite_has_30_valid_tasks() -> None:
    tasks = load_suite(SUITE)
    assert len(tasks) == 30
    assert len({task.id for task in tasks}) == 30
    assert all((task.directory / "solution").is_dir() for task in tasks)


@pytest.mark.parametrize("task", load_suite(SUITE), ids=lambda task: task.id)
def test_each_seeded_task_fails_as_shipped_and_passes_with_its_solution(task, tmp_path) -> None:
    runner = UnconfinedHiddenTestRunner()
    fixture = tmp_path / "repo"
    materialize_fixture(task, fixture)
    assert hidden_tests_absent(task, fixture)
    broken = runner.run(task, prepare_hidden_tree(task, fixture, tmp_path / "broken"), outcome=None)
    assert not broken.passed and broken.boundary == "UNCONFINED", broken.output_tail
    shutil.copytree(task.directory / "solution", fixture, dirs_exist_ok=True)
    fixed = runner.run(task, prepare_hidden_tree(task, fixture, tmp_path / "fixed"), outcome=None)
    assert fixed.passed, fixed.output_tail


def _run(tmp_path: Path, name: str, *extra: str) -> Path:
    out = tmp_path / f"{name}.json"
    result = CliRunner().invoke(app, [
        "eval", "run", "--suite", str(SUITE), "--agent", "mock", "--k", "2", "--name", name,
        "--allow-unconfined-hidden-tests", "--out", str(out),
        "--task", "op-add", "--task", "range-sum-5", "--task", "str-reverse",
        "--task", "empty-largest", "--task", "clamp-0", "--task", "fizzbuzz-3-5", *extra])
    assert result.exit_code == 0, result.output
    return out


def test_eval_run_compare_and_report_end_to_end(tmp_path: Path) -> None:
    good = _run(tmp_path, "good", "--seed", "1", "--mock-skill", "1.0", "--html",
                str(tmp_path / "good.html"))
    rerun = _run(tmp_path, "rerun", "--seed", "2", "--mock-skill", "1.0")
    broken = _run(tmp_path, "broken", "--seed", "3", "--mock-skill", "1.0",
                  "--prompt-template", "Please improve this repository.")

    document = load_document(good)
    summary = summarize(document)
    assert summary.attempts == 12 and summary.passes == 12
    assert summary.hidden_boundaries == {"UNCONFINED": 12}
    assert all(item["hidden_tests_absent"] for item in document["results"])
    page = (tmp_path / "good.html").read_text(encoding="utf-8")
    assert "12/12 passed" in page and "<script" not in page
    assert "Hidden tests ran under: UNCONFINED: 12" in page

    runner = CliRunner()
    same = runner.invoke(app, ["eval", "compare", str(good), str(rerun), "--fail-on-regression",
                               "--json"])
    assert same.exit_code == 0, same.output
    assert json.loads(same.output)["regression"] is False

    regressed = runner.invoke(app, ["eval", "compare", str(good), str(broken),
                                    "--fail-on-regression"])
    assert regressed.exit_code == EXIT_REGRESSION, regressed.output
    assert "REGRESSION" in regressed.output and "regressed:" in regressed.output

    report = runner.invoke(app, ["eval", "report", str(broken), "--html",
                                 str(tmp_path / "broken.html")])
    assert report.exit_code == 0
    assert "0/12 passed" in (tmp_path / "broken.html").read_text(encoding="utf-8")


def test_without_a_confined_runner_the_cli_refuses_rather_than_running_unconfined(
        tmp_path: Path, monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    result = CliRunner().invoke(app, ["eval", "run", "--suite", str(SUITE), "--agent", "mock",
                                      "--task", "op-add", "--out", str(tmp_path / "x.json")])
    assert result.exit_code != 0
    import re
    # Rich boxes and colours the message and may wrap it; compare the letters only.
    plain = re.sub(r"\x1b\[[0-9;]*m|[^a-z]", "", result.output.lower())
    assert "allowunconfinedhiddentests" in plain
    assert not (tmp_path / "x.json").exists()


def test_report_html_escapes_untrusted_task_ids(tmp_path: Path) -> None:
    document = {"schema": "sentinel-eval-run/1", "id": "r", "suite": "<s>", "k": 1,
                "config": {"name": "<img src=x onerror=alert(1)>", "agent": "mock"},
                "started_at": "t", "completed_at": "t",
                "results": [{"task_id": "<b>t</b>", "status": "PASSED", "wall_seconds": 1.0,
                             "agent": None, "hidden": {"boundary": "UNCONFINED"}}]}
    page = render_html(document)
    assert "<img" not in page and "<b>t</b>" not in page and "&lt;b&gt;" in page
