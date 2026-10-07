import pytest
from typer.testing import CliRunner

from backend.app.cli.main import app
from backend.app.evals.leaderboard import render_leaderboard
from backend.app.evals.report import write_document


def document():
    return {"schema": "sentinel-eval-run/1", "id": "run-1", "suite": "suite",
            "config": {"name": "<script>alert(1)</script>", "agent": "mock"},
            "k": 1, "started_at": "2026-10-04", "completed_at": None,
            "results": [{"task_id": "task-1", "status": "ERROR", "wall_seconds": 1,
                         "agent": None, "hidden": None}]}


def test_honest_metrics_and_html_escaping():
    page = render_leaderboard([document()])
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "0/1 (0.0%)" in page
    assert "UNKNOWN (1 unknown)" in page
    assert "INCOMPLETE" in page
    assert "NONE: 1" in page


def test_duplicate_runs_are_rejected():
    with pytest.raises(ValueError, match="duplicate run"):
        render_leaderboard([document(), document()])


def test_cli_generates_static_page(tmp_path):
    source, target = tmp_path / "run.json", tmp_path / "pages" / "index.html"
    write_document(document(), source)
    result = CliRunner().invoke(app, ["eval", "leaderboard", str(source), "--html", str(target)])
    assert result.exit_code == 0, result.output
    assert "run-1" in target.read_text()


def test_pages_workflow_builds_from_committed_results(tmp_path):
    """Every results file the Pages workflow publishes exists, loads and renders."""
    import re
    import shlex
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    workflow = (root / ".github" / "workflows" / "leaderboard.yml").read_text(encoding="utf-8")
    block = workflow[workflow.index("sentinel eval leaderboard"):workflow.index("--html _site")]
    inputs = [part for part in shlex.split(block.replace("\\\n", " "))[3:]]
    assert len(inputs) == 5 and all(path.endswith(".json") for path in inputs)
    target = tmp_path / "_site" / "index.html"
    result = CliRunner().invoke(app, ["eval", "leaderboard", *[str(root / path) for path in inputs],
                                      "--html", str(target)])
    assert result.exit_code == 0, result.output
    page = target.read_text(encoding="utf-8")
    for name in ("baseline-a", "baseline-b", "degraded", "broken-prompt"):
        assert name in page
    assert "LINUX_SANDBOX: 90" in page and "UNCONFINED: 1" in page
    names = re.search(r"for name in ([^;]+);", workflow).group(1).split()
    for name in names:
        report = tmp_path / "_site" / "runs" / f"{name}.html"
        source = root / "bench" / "regression_detection" / "results" / f"{name}.json"
        result = CliRunner().invoke(app, ["eval", "report", str(source), "--html", str(report)])
        assert result.exit_code == 0, result.output
        assert "Hidden tests ran under: LINUX_SANDBOX: 90" in report.read_text(encoding="utf-8")
