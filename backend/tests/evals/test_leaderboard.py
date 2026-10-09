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
