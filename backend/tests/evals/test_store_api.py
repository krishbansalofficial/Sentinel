"""Recorded eval runs over the real API: store, list, get, refusals, journal events."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.credentials.memory_store import InMemoryCredentialStore
from backend.app.main import create_app


def _document(results: list[dict], **overrides) -> dict:
    document = {"schema": "sentinel-eval-run/1", "id": str(uuid4()), "suite": "evals/tasks",
                "config": {"name": "mock-good", "agent": "mock", "model": None,
                           "prompt_template": "{prompt}", "settings": {}},
                "k": 2, "started_at": "2026-10-06T10:00:00+00:00",
                "completed_at": "2026-10-06T10:05:00+00:00", "results": results}
    document.update(overrides)
    return document


def _result(task: str, attempt: int, status: str, *, change_id: str | None = None,
            cost: float | None = 0.01) -> dict:
    return {"run_id": "x", "task_id": task, "attempt": attempt, "status": status,
            "hidden_tests_absent": True, "wall_seconds": 1.5 + attempt,
            "agent": {"status": "PASSED", "wall_seconds": 1.0, "cost_usd": cost,
                      "change_id": change_id},
            "hidden": {"passed": status == "PASSED", "boundary": "LINUX_SANDBOX"},
            "error": None, "started_at": "2026-10-06T10:00:00+00:00"}


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(settings=Settings(database_path=tmp_path / "s" / "db.sqlite3"),
                     credential_store=InMemoryCredentialStore())
    with TestClient(app) as http:
        http.headers["Authorization"] = f"Bearer {app.state.api_token}"
        yield http


def test_record_list_and_get_a_run(client) -> None:
    results = [_result("a", 1, "PASSED"), _result("a", 2, "FAILED", cost=None),
               _result("b", 1, "PASSED"), _result("b", 2, "ERROR")]
    document = _document(results)
    created = client.post("/api/v1/evals/runs", json=document)
    assert created.status_code == 201, created.text
    view = created.json()
    assert (view["passes"], view["attempts"], view["errors"]) == (2, 4, 1)
    assert view["rate"] == 0.5 and 0.0 < view["low"] < 0.5 < view["high"] < 1.0
    assert view["mean_cost_usd"] == pytest.approx(0.01) and view["cost_unknown"] == 1
    assert view["hidden_boundaries"] == {"LINUX_SANDBOX": 4}
    assert [task["task_id"] for task in view["tasks"]] == ["a", "b"]
    listed = client.get("/api/v1/evals/runs").json()
    assert listed["count"] == 1 and listed["items"][0]["id"] == document["id"]
    assert client.get(f"/api/v1/evals/runs/{document['id']}").json() == view


def test_duplicates_unknown_and_malformed_runs_are_refused(client) -> None:
    document = _document([_result("a", 1, "PASSED")])
    assert client.post("/api/v1/evals/runs", json=document).status_code == 201
    duplicate = client.post("/api/v1/evals/runs", json=document)
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "EVAL_RUN_EXISTS"
    missing = client.get(f"/api/v1/evals/runs/{uuid4()}")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "EVAL_RUN_NOT_FOUND"
    assert client.post("/api/v1/evals/runs",
                       json=_document([], schema="other/1")).status_code == 422
    bad = client.post("/api/v1/evals/runs", json=_document([{"task_id": "a"}]))
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "EVAL_RUN_INVALID"
    assert client.post("/api/v1/evals/runs", json={**_document([]), "extra": 1}).status_code == 422


def test_attempts_on_a_real_change_are_journaled_on_that_change(client) -> None:
    repo_path = Path(client.app.state.settings.database_path).parent.parent / "repo"
    from backend.tests.support_kb import make_repo
    make_repo(repo_path)
    change = client.post("/api/v1/changes", json={"title": "eval", "intent": "attempt",
                                                  "repository_path": str(repo_path)}).json()
    document = _document([_result("a", 1, "PASSED", change_id=change["id"]),
                          _result("a", 2, "FAILED", change_id=str(uuid4()))])
    assert client.post("/api/v1/evals/runs", json=document).status_code == 201
    events = client.get(f"/api/v1/changes/{change['id']}/events").json()["items"]
    recorded = [event for event in events if event["event_type"] == "eval.result_recorded"]
    assert len(recorded) == 1
    assert recorded[0]["payload"]["task_id"] == "a" and recorded[0]["payload"]["status"] == "PASSED"
    assert recorded[0]["subject_id"] == document["id"]
    assert client.get(f"/api/v1/changes/{change['id']}/replay/verify").json()["verified"] is True


def test_attempts_are_listed_with_unknown_fields_left_null(client) -> None:
    failed = _result("a", 2, "ERROR", cost=None)
    failed.update(error="RuntimeError: boom", hidden=None)
    document = _document([_result("a", 1, "PASSED"), failed])
    assert client.post("/api/v1/evals/runs", json=document).status_code == 201
    attempts = client.get(f"/api/v1/evals/runs/{document['id']}/attempts").json()
    assert attempts["count"] == 2
    first, second = attempts["items"]
    assert (first["task_id"], first["attempt"], first["status"]) == ("a", 1, "PASSED")
    assert first["hidden_boundary"] == "LINUX_SANDBOX" and first["cost_usd"] == 0.01
    assert second["status"] == "ERROR" and second["error"] == "RuntimeError: boom"
    assert second["hidden_boundary"] is None and second["cost_usd"] is None
    missing = client.get(f"/api/v1/evals/runs/{uuid4()}/attempts")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "EVAL_RUN_NOT_FOUND"


def _run(tasks: int, passes_per_task: int, k: int = 3) -> dict:
    return _document([_result(f"t{task:02d}", attempt, "PASSED" if attempt <= passes_per_task
                              else "FAILED")
                      for task in range(tasks) for attempt in range(1, k + 1)], k=k)


def test_compare_matches_the_cli_and_flags_only_a_real_regression(client) -> None:
    from backend.app.evals.report import compare

    good, same, broken = _run(20, 3), _run(20, 3), _run(20, 0)
    for document in (good, same, broken):
        assert client.post("/api/v1/evals/runs", json=document).status_code == 201
    regression = client.get("/api/v1/evals/compare",
                            params={"baseline": good["id"], "candidate": broken["id"]}).json()
    expected = compare(good, broken)
    assert regression["regression"] is True and regression["intervals_separate"] is True
    assert regression["overall_delta"] == pytest.approx(-1.0)
    assert regression["bootstrap"]["mean_difference"] == pytest.approx(
        expected.bootstrap.mean_difference)
    assert [task["flip"] for task in regression["tasks"]] == ["regressed"] * 20
    steady = client.get("/api/v1/evals/compare",
                        params={"baseline": good["id"], "candidate": same["id"]}).json()
    assert steady["regression"] is False and steady["overall_delta"] == 0.0
    assert all(task["flip"] is None for task in steady["tasks"])
    missing = client.get("/api/v1/evals/compare",
                         params={"baseline": good["id"], "candidate": str(uuid4())})
    assert missing.status_code == 404


def test_compare_reports_tasks_only_in_one_run(client) -> None:
    a = _document([_result("a", 1, "PASSED"), _result("b", 1, "PASSED")], k=1)
    b = _document([_result("b", 1, "PASSED"), _result("c", 1, "FAILED")], k=1)
    for document in (a, b):
        assert client.post("/api/v1/evals/runs", json=document).status_code == 201
    view = client.get("/api/v1/evals/compare",
                      params={"baseline": a["id"], "candidate": b["id"]}).json()
    assert view["only_in_a"] == ["a"] and view["only_in_b"] == ["c"]
    assert [task["task_id"] for task in view["tasks"]] == ["b"]
