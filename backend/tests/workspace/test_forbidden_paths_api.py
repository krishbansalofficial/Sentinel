"""Forbidden-path apply refusal over the real HTTP API (real AppContainer profiles and Git).

The contract is set through ``PUT /changes/{id}/contract``; the workspace is built
like the other route tests (``ensure`` + host edits + ``finish_run``).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.app.workspace.models import FORBIDDEN_FLAG, ApplyRefusal
from backend.tests.support_kb import write
from backend.tests.workspace.test_routes import (  # noqa: F401 (api is a fixture)
    FACTS, _head, _repo, _setup, _workspace_with_edits, api, windows_only,
)

pytestmark = windows_only


def _forbid(client, change: dict, patterns: list[str]) -> dict:
    current = client.get(f"/api/v1/changes/{change['id']}").json()
    contract = {**current["contract"], "forbidden_paths": patterns}
    response = client.put(f"/api/v1/changes/{change['id']}/contract", json={
        "contract": contract, "expected_revision": current["revision"]})
    assert response.status_code == 200, response.text
    return response.json()


def _flags(body: dict, path: str) -> list[str]:
    return next(item["flags"] for item in body["changed_paths"] if item["path"] == path)


@pytest.mark.parametrize("pattern, hit", [
    ("notes.txt", "notes.txt"),
    ("*.txt", "notes.txt"),
    ("**/*.py", "calc.py"),
])
def test_preview_refuses_contract_forbidden_paths(api, tmp_path, pattern, hit) -> None:
    app, client = api
    repo = _repo(tmp_path)
    before = _head(repo)
    change, _agent, _human, _delegation = _setup(client, repo, ["workspace.apply"])
    _forbid(client, change, [pattern])
    _workspace_with_edits(app, change["id"], repo)

    body = client.post(f"/api/v1/changes/{change['id']}/workspace/preview").json()

    assert body["refusal_reason"] == ApplyRefusal.FORBIDDEN_PATH_IN_DIFF.value
    assert body["approval_token"] is None and body["fast_forward_possible"] is False
    assert FORBIDDEN_FLAG in _flags(body, hit)
    assert _head(repo) == before


def test_unrelated_forbidden_pattern_still_issues_a_token(api, tmp_path) -> None:
    app, client = api
    repo = _repo(tmp_path)
    change, _agent, _human, _delegation = _setup(client, repo, ["workspace.apply"])
    _forbid(client, change, ["secrets/**", "*.pem"])
    _workspace_with_edits(app, change["id"], repo)
    body = client.post(f"/api/v1/changes/{change['id']}/workspace/preview").json()
    assert body["refusal_reason"] is None and body["approval_token"]
    assert all(FORBIDDEN_FLAG not in item["flags"] for item in body["changed_paths"])


def test_deleting_a_forbidden_file_is_refused(api, tmp_path) -> None:
    app, client = api
    repo = _repo(tmp_path)
    change, _agent, _human, _delegation = _setup(client, repo, ["workspace.apply"])
    _forbid(client, change, ["README.md"])
    manager = app.state.workspace_manager
    run_id = uuid4()
    record = manager.ensure(UUID(change["id"]), str(repo), run_id=run_id)
    (record.workspace_path / "README.md").unlink()
    write(record.workspace_path, "calc.py", "def add(a, b):\n    return b + a\n")
    manager.finish_run(record.id, run_id, facts={**FACTS, "profile_name": record.profile_name},
                       status="COMPLETED")
    body = client.post(f"/api/v1/changes/{change['id']}/workspace/preview").json()
    assert body["refusal_reason"] == ApplyRefusal.FORBIDDEN_PATH_IN_DIFF.value
    assert body["approval_token"] is None


def test_contract_tightened_after_preview_refuses_apply(api, tmp_path) -> None:
    app, client = api
    repo = _repo(tmp_path)
    before = _head(repo)
    change, agent, _human, _delegation = _setup(client, repo, ["workspace.apply"])
    base = f"/api/v1/changes/{change['id']}/workspace"
    _workspace_with_edits(app, change["id"], repo)
    preview = client.post(f"{base}/preview").json()
    assert preview["approval_token"] and preview["refusal_reason"] is None

    _forbid(client, change, ["calc.py"])
    applied = client.post(f"{base}/apply", json={
        "actor_id": agent, "approval_token": preview["approval_token"]})

    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["applied"] is False
    assert result["workspace"]["refusal_reason"] == ApplyRefusal.FORBIDDEN_PATH_IN_DIFF.value
    assert _head(repo) == before
    # The voided approval cannot be replayed.
    again = client.post(f"{base}/apply", json={
        "actor_id": agent, "approval_token": preview["approval_token"]})
    assert again.status_code != 200 or again.json()["applied"] is False
    assert _head(repo) == before
