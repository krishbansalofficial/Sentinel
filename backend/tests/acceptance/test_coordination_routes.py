from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.tests.core.fakes import FakeGitInspection, FakeVerification
from backend.app.contracts.models import RepositoryInfo


def build_client(tmp_path) -> TestClient:
    git = FakeGitInspection(
        RepositoryInfo(root="C:\\canonical repo", branch="main", head_sha="a" * 40),
        None,
    )
    app = create_app(
        settings=Settings(database_path=tmp_path / "coord.sqlite3"),
        git_inspection=git,
        verification=FakeVerification(None),
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return client


def _create_change(client: TestClient) -> str:
    response = client.post(
        "/api/v1/changes",
        json={
            "title": "Coordination smoke",
            "intent": "Exercise task routes",
            "repository_path": "C:\\canonical repo",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_capabilities_reports_task_coordination(tmp_path) -> None:
    with build_client(tmp_path) as client:
        response = client.get("/api/v1/capabilities")
        assert response.status_code == 200
        ids = {item["id"] for item in response.json()["items"]}
        assert "task_coordination" in ids


def test_task_create_list_get(tmp_path) -> None:
    with build_client(tmp_path) as client:
        change_id = _create_change(client)
        create = client.post(
            f"/api/v1/changes/{change_id}/tasks",
            json={
                "title": "Write the parser",
                "instructions": "Handle the edge cases",
                "adapter": "claude",
            },
        )
        assert create.status_code == 201, create.text
        task = create.json()
        assert task["state"] == "DRAFT"
        assert task["revision"] == 1

        listed = client.get(f"/api/v1/changes/{change_id}/tasks")
        assert listed.status_code == 200
        assert listed.json()["count"] == 1

        fetched = client.get(f"/api/v1/changes/{change_id}/tasks/{task['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == task["id"]


def test_task_routes_404_for_unknown_change(tmp_path) -> None:
    with build_client(tmp_path) as client:
        import uuid

        response = client.get(f"/api/v1/changes/{uuid.uuid4()}/tasks")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CHANGE_NOT_FOUND"


def test_task_edit_revision_conflict(tmp_path) -> None:
    with build_client(tmp_path) as client:
        change_id = _create_change(client)
        task = client.post(
            f"/api/v1/changes/{change_id}/tasks",
            json={"title": "T", "instructions": "I", "adapter": "claude"},
        ).json()

        stale = client.patch(
            f"/api/v1/changes/{change_id}/tasks/{task['id']}",
            json={"expected_revision": 99, "title": "New title"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "TASK_REVISION_CONFLICT"

        ok = client.patch(
            f"/api/v1/changes/{change_id}/tasks/{task['id']}",
            json={"expected_revision": 1, "title": "New title"},
        )
        assert ok.status_code == 200
        assert ok.json()["title"] == "New title"


def test_dependency_cycle_rejected_over_http(tmp_path) -> None:
    with build_client(tmp_path) as client:
        change_id = _create_change(client)
        a = client.post(
            f"/api/v1/changes/{change_id}/tasks",
            json={"title": "A", "instructions": "I", "adapter": "claude"},
        ).json()
        b = client.post(
            f"/api/v1/changes/{change_id}/tasks",
            json={"title": "B", "instructions": "I", "adapter": "claude"},
        ).json()

        first = client.put(
            f"/api/v1/changes/{change_id}/tasks/{b['id']}/dependencies",
            json={"expected_revision": 1, "depends_on_task_ids": [a["id"]]},
        )
        assert first.status_code == 200, first.text

        second = client.put(
            f"/api/v1/changes/{change_id}/tasks/{a['id']}/dependencies",
            json={"expected_revision": 1, "depends_on_task_ids": [b["id"]]},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "TASK_DEPENDENCY_CYCLE"


def test_submit_then_cancel_flow_over_http(tmp_path) -> None:
    with build_client(tmp_path) as client:
        change_id = _create_change(client)
        task = client.post(
            f"/api/v1/changes/{change_id}/tasks",
            json={"title": "T", "instructions": "I", "adapter": "claude"},
        ).json()

        submitted = client.post(
            f"/api/v1/changes/{change_id}/tasks/{task['id']}/submit",
            json={"expected_revision": 1},
        )
        assert submitted.status_code == 200
        assert submitted.json()["state"] == "READY"

        cancelled = client.post(
            f"/api/v1/changes/{change_id}/tasks/{task['id']}/cancel",
            json={"expected_revision": 2, "reason": "not needed"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "CANCELLED"


def test_idempotency_key_replay_over_http(tmp_path) -> None:
    with build_client(tmp_path) as client:
        change_id = _create_change(client)
        body = {"title": "T", "instructions": "I", "adapter": "claude"}
        headers = {"Idempotency-Key": "task-create-1"}

        first = client.post(
            f"/api/v1/changes/{change_id}/tasks", json=body, headers=headers
        )
        second = client.post(
            f"/api/v1/changes/{change_id}/tasks", json=body, headers=headers
        )
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

        listed = client.get(f"/api/v1/changes/{change_id}/tasks")
        assert listed.json()["count"] == 1


def test_task_execution_fields_round_trip_over_http(tmp_path) -> None:
    """Regression: these fields were accepted by the API and silently dropped."""

    body = {
        "title": "Run the agent",
        "instructions": "Fix the parser",
        "adapter": "claude",
        "executable": "python",
        "args": ["-m", "agent", "{instructions_file}"],
        "write_paths": ["src/**", "tests/**"],
        "verification": [{"executable": "pytest", "args": ["-q"], "timeout_seconds": 90}],
        "resources": [{"key": "port:5432", "units": 1}],
    }
    with build_client(tmp_path) as client:
        change_id = _create_change(client)
        created = client.post(f"/api/v1/changes/{change_id}/tasks", json=body)
        assert created.status_code == 201, created.text
        task = created.json()
        for key in ("executable", "args", "write_paths", "verification", "resources"):
            assert task[key] == body[key], key

        fetched = client.get(f"/api/v1/changes/{change_id}/tasks/{task['id']}").json()
        assert fetched["args"] == body["args"]

        edited = client.patch(
            f"/api/v1/changes/{change_id}/tasks/{task['id']}",
            json={"expected_revision": 1, "args": [], "write_paths": ["docs/**"],
                  "resources": []},
        )
        assert edited.status_code == 200, edited.text
        after = edited.json()
        assert after["args"] == [] and after["write_paths"] == ["docs/**"]
        assert after["resources"] == [] and after["executable"] == "python"
        assert after["verification"] == body["verification"]


def test_duplicate_resource_keys_are_rejected(tmp_path) -> None:
    with build_client(tmp_path) as client:
        change_id = _create_change(client)
        response = client.post(f"/api/v1/changes/{change_id}/tasks", json={
            "title": "t", "instructions": "i", "adapter": "claude",
            "resources": [{"key": "port:1"}, {"key": "port:1"}],
        })
        assert response.status_code == 422
