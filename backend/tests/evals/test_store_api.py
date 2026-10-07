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
