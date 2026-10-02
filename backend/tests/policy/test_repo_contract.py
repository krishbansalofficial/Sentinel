"""A repository Change Contract is read only from the baseline commit, strictly validated."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.core.errors import AppError
from backend.app.credentials.memory_store import InMemoryCredentialStore
from backend.app.main import create_app
from backend.app.policy.repo_contract import (
    CONTRACT_PATH, MAX_CONTRACT_BYTES, read_baseline_contract,
)
from backend.tests.support_kb import git, make_repo, write

VALID = """\
schema_version = 3
allowed_paths = ["src/**", "tests/**"]
forbidden_paths = [".github/**", "*.pem"]
required_checks = ["pytest"]
max_risk = "LOW"
policy_preset_name = "standard"
policy_change_type = "code"
"""


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def _repo(tmp_path: Path, contract: str | None = VALID) -> Path:
    files = {"README.md": "hello\n"}
    if contract is not None:
        files[CONTRACT_PATH] = contract
    return make_repo(tmp_path / "repo", files)


def _code(callable_) -> str:
    with pytest.raises(AppError) as caught:
        callable_()
    return caught.value.code


def test_committed_contract_is_parsed_and_digested(tmp_path) -> None:
    repo = _repo(tmp_path)
    loaded = read_baseline_contract(repo, _head(repo))
    assert loaded.contract.forbidden_paths == [".github/**", "*.pem"]
    assert loaded.contract.max_risk.value == "LOW"
    assert loaded.contract.policy_preset_name == "standard"
    assert loaded.commit == _head(repo) and loaded.path == CONTRACT_PATH
    assert loaded.blob_sha256 == hashlib.sha256(VALID.encode()).hexdigest()


def test_working_tree_and_later_commits_never_change_the_baseline_contract(tmp_path) -> None:
    repo = _repo(tmp_path)
    baseline = _head(repo)
    loosened = VALID.replace('forbidden_paths = [".github/**", "*.pem"]', "forbidden_paths = []")
    write(repo, CONTRACT_PATH, loosened)  # uncommitted agent edit
    assert read_baseline_contract(repo, baseline).contract.forbidden_paths == [".github/**", "*.pem"]
    git(repo, "commit", "-qam", "agent loosens its contract")
    assert read_baseline_contract(repo, baseline).contract.forbidden_paths == [".github/**", "*.pem"]
    assert read_baseline_contract(repo, _head(repo)).contract.forbidden_paths == []


def test_missing_contract_is_its_own_code(tmp_path) -> None:
    repo = _repo(tmp_path, contract=None)
    assert _code(lambda: read_baseline_contract(repo, _head(repo))) == "REPOSITORY_CONTRACT_MISSING"


@pytest.mark.parametrize("baseline", ["", "HEAD", "main", "0" * 40, "g" * 40, "a" * 39])
def test_baseline_must_be_an_existing_commit_id(tmp_path, baseline) -> None:
    repo = _repo(tmp_path)
    assert _code(lambda: read_baseline_contract(repo, baseline)) == "REPOSITORY_CONTRACT_UNAVAILABLE"


def _index_entry(repo: Path, mode: str, oid: str) -> None:
    git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{CONTRACT_PATH}")
    git(repo, "commit", "-qm", f"contract as {mode}")


def test_symlinked_contract_is_refused(tmp_path) -> None:
    repo = _repo(tmp_path, contract=None)
    target = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input="../README.md",
                            capture_output=True, text=True, check=True).stdout.strip()
    _index_entry(repo, "120000", target)
    assert _code(lambda: read_baseline_contract(repo, _head(repo))) == "REPOSITORY_CONTRACT_INVALID"


def test_gitlink_contract_is_refused(tmp_path) -> None:
    repo = _repo(tmp_path, contract=None)
    _index_entry(repo, "160000", _head(repo))
    assert _code(lambda: read_baseline_contract(repo, _head(repo))) == "REPOSITORY_CONTRACT_INVALID"


def test_directory_named_like_the_contract_is_refused(tmp_path) -> None:
    repo = _repo(tmp_path, contract=None)
    write(repo, CONTRACT_PATH + "/inner.toml", VALID)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "directory")
    assert _code(lambda: read_baseline_contract(repo, _head(repo))) == "REPOSITORY_CONTRACT_INVALID"


@pytest.mark.parametrize("body, needle", [
    ("max_risk = [", "TOML"),
    ('max_risk = "LOW"\nunexpected_key = 1\n', "unexpected_key"),
    ('max_risk = "EXTREME"\n', "max_risk"),
    ('policy_preset_name = "standard"\npolicy_change_type = "code"\n', "schema version"),
    ('forbidden_paths = ["a", "a"]\n', "forbidden_paths"),
])
def test_invalid_contracts_name_the_problem(tmp_path, body, needle) -> None:
    repo = _repo(tmp_path, contract=body)
    with pytest.raises(AppError) as caught:
        read_baseline_contract(repo, _head(repo))
    assert caught.value.code == "REPOSITORY_CONTRACT_INVALID"
    text = caught.value.message + " ".join(caught.value.details.get("problems", []))
    assert needle.casefold() in text.casefold(), text


def test_non_utf8_and_oversized_contracts_are_refused(tmp_path) -> None:
    repo = _repo(tmp_path, contract=None)
    (repo / ".sentinel").mkdir()
    (repo / CONTRACT_PATH).write_bytes(b'max_risk = "LOW"\n# \xff\xfe\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "latin-1")
    assert _code(lambda: read_baseline_contract(repo, _head(repo))) == "REPOSITORY_CONTRACT_INVALID"
    (repo / CONTRACT_PATH).write_bytes(b"# " + b"x" * MAX_CONTRACT_BYTES + b'\nmax_risk = "LOW"\n')
    git(repo, "commit", "-qam", "huge")
    assert _code(lambda: read_baseline_contract(repo, _head(repo))) == "REPOSITORY_CONTRACT_INVALID"


# ------------------------------------------------------------------------------- HTTP


@pytest.fixture
def client(tmp_path):
    app = create_app(settings=Settings(database_path=tmp_path / "state" / "api.sqlite3"),
                     credential_store=InMemoryCredentialStore())
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        yield app, test_client


def _change(client: TestClient, repo: Path) -> dict:
    response = client.post("/api/v1/changes", json={
        "title": "repo contract", "intent": "load the committed contract",
        "repository_path": str(repo)})
    assert response.status_code == 201, response.text
    return response.json()


def test_api_applies_the_baseline_contract_and_journals_its_source(client, tmp_path) -> None:
    app, http = client
    repo = _repo(tmp_path)
    change = _change(http, repo)
    assert http.post(f"/api/v1/changes/{change['id']}/evidence/baseline").status_code == 201
    write(repo, CONTRACT_PATH, VALID.replace('"LOW"', '"HIGH"'))  # agent edit after baseline
    current = http.get(f"/api/v1/changes/{change['id']}").json()

    response = http.post(f"/api/v1/changes/{change['id']}/contract/from-repository",
                         json={"expected_revision": current["revision"]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["change"]["contract"]["max_risk"] == "LOW"
    assert body["change"]["contract"]["forbidden_paths"] == [".github/**", "*.pem"]
    assert body["commit"] == _head(repo) and body["path"] == CONTRACT_PATH
    with app.state.change_service.repository.database.connection() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM journal_events WHERE change_id = ? AND event_type = ?",
            (change["id"], "change.contract_updated")).fetchall()
    assert any('"blob_sha256"' in row["payload_json"] and body["commit"] in row["payload_json"]
               for row in rows)


def test_api_requires_a_baseline_and_the_current_revision(client, tmp_path) -> None:
    _app, http = client
    repo = _repo(tmp_path)
    change = _change(http, repo)
    url = f"/api/v1/changes/{change['id']}/contract/from-repository"
    missing = http.post(url, json={"expected_revision": change["revision"]})
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "REPOSITORY_CONTRACT_UNAVAILABLE"
    assert http.post(f"/api/v1/changes/{change['id']}/evidence/baseline").status_code == 201
    stale = http.post(url, json={"expected_revision": change["revision"] + 5})
    assert stale.status_code == 409
    unchanged = http.get(f"/api/v1/changes/{change['id']}").json()["contract"]
    assert unchanged["max_risk"] == "MEDIUM"


def test_api_reports_a_missing_contract_without_changing_anything(client, tmp_path) -> None:
    _app, http = client
    repo = _repo(tmp_path, contract=None)
    change = _change(http, repo)
    assert http.post(f"/api/v1/changes/{change['id']}/evidence/baseline").status_code == 201
    current = http.get(f"/api/v1/changes/{change['id']}").json()
    response = http.post(f"/api/v1/changes/{change['id']}/contract/from-repository",
                         json={"expected_revision": current["revision"]})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPOSITORY_CONTRACT_MISSING"
    assert http.get(f"/api/v1/changes/{change['id']}").json()["revision"] == current["revision"]
