"""GitLab CI outcomes: commit statuses for one SHA, fail-safe mapping, routed by origin remote."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.core.errors import AppError
from backend.app.credentials.memory_store import InMemoryCredentialStore
from backend.app.main import create_app
from backend.app.outcomes.tracker import OutcomeTracker
from backend.app.providers.gitlab import MAX_PAGES, PER_PAGE, GitLabProvider, map_status
from backend.app.providers.http_transport import TransportTimeout
from backend.app.providers.models import CheckConclusion
from backend.app.providers.repository_slug import gitlab_project_path, resolve_gitlab_project_path
from backend.tests.providers.fakes import FakeHttpTransport, json_response
from backend.tests.support_kb import git, make_repo

SHA = "a" * 40
OTHER = "b" * 40
FIXED = datetime(2026, 10, 2, tzinfo=UTC)


def _status(name: str, status: str, sha: str = SHA) -> dict:
    return {"id": 1, "name": name, "status": status, "sha": sha, "ref": "main"}


def _provider(queue) -> tuple[GitLabProvider, FakeHttpTransport]:
    transport = FakeHttpTransport(queue)
    return GitLabProvider(transport, clock=lambda: FIXED), transport


# ------------------------------------------------------------------------ provider


@pytest.mark.parametrize("status, expected", [
    ("success", CheckConclusion.SUCCESS), ("failed", CheckConclusion.FAILURE),
    ("canceled", CheckConclusion.CANCELLED), ("skipped", CheckConclusion.NEUTRAL),
    ("pending", CheckConclusion.PENDING), ("running", CheckConclusion.PENDING),
    ("created", CheckConclusion.PENDING), ("manual", CheckConclusion.PENDING),
    ("scheduled", CheckConclusion.PENDING), ("waiting_for_resource", CheckConclusion.PENDING),
    ("SUCCESS", CheckConclusion.PENDING), ("", CheckConclusion.PENDING), (None, CheckConclusion.PENDING),
    (1, CheckConclusion.PENDING),
])
def test_only_finished_states_have_a_conclusion(status, expected) -> None:
    assert map_status(status) is expected


def test_statuses_are_listed_for_the_encoded_nested_project() -> None:
    provider, transport = _provider([json_response(200, [_status("test", "success"),
                                                        _status("lint", "failed")])])
    runs = provider.list_check_runs_for_sha(token="tok", repository="grp/sub/proj", head_sha=SHA)
    assert [(run.name, run.conclusion) for run in runs] == [
        ("test", CheckConclusion.SUCCESS), ("lint", CheckConclusion.FAILURE)]
    call = transport.calls[0]
    assert call["method"] == "GET" and call["headers"]["Authorization"] == "Bearer tok"
    assert "/projects/grp%2Fsub%2Fproj/repository/commits/" + SHA + "/statuses" in call["url"]
    assert unquote(call["url"]).count("grp/sub/proj") == 1


def test_pagination_continues_on_full_pages_and_is_bounded() -> None:
    full = [_status(f"job-{i}", "success") for i in range(PER_PAGE)]
    provider, transport = _provider([json_response(200, full), json_response(200, [_status("last", "failed")])])
    runs = provider.list_check_runs_for_sha(token="t", repository="g/p", head_sha=SHA)
    assert len(runs) == PER_PAGE + 1 and len(transport.calls) == 2
    assert "page=2" in transport.calls[1]["url"]
    provider, transport = _provider([json_response(200, full)] * (MAX_PAGES + 5))
    provider.list_check_runs_for_sha(token="t", repository="g/p", head_sha=SHA)
    assert len(transport.calls) == MAX_PAGES


def test_malformed_records_are_dropped_not_trusted() -> None:
    provider, _ = _provider([json_response(200, [
        {"name": "no-sha", "status": "success"}, {"status": "success", "sha": SHA},
        "not-an-object", {"name": "bad-sha", "status": "success", "sha": "xyz"},
        _status("good", "success")])])
    runs = provider.list_check_runs_for_sha(token="t", repository="g/p", head_sha=SHA)
    assert [run.name for run in runs] == ["good"]


@pytest.mark.parametrize("response, code", [
    (json_response(401, {"message": "401 Unauthorized"}), "PROVIDER_AUTH_FAILED"),
    (json_response(403, {"message": "403 Forbidden"}), "PROVIDER_AUTH_FAILED"),
    (json_response(404, {"message": "404 Project Not Found"}), "PROVIDER_NOT_FOUND"),
    (json_response(400, {"message": "bad"}), "PROVIDER_UNAVAILABLE"),
])
def test_http_errors_map_to_stable_codes(response, code) -> None:
    provider, _ = _provider([response])
    with pytest.raises(AppError) as caught:
        provider.list_check_runs_for_sha(token="t", repository="g/p", head_sha=SHA)
    assert caught.value.code == code


def test_non_list_and_non_json_bodies_are_unavailable() -> None:
    for body in ({"message": "not a list"}, "plain"):
        provider, _ = _provider([json_response(200, body)])
        with pytest.raises(AppError):
            provider.list_check_runs_for_sha(token="t", repository="g/p", head_sha=SHA)


def test_rate_limits_server_errors_and_timeouts_retry_then_give_up() -> None:
    provider, transport = _provider([
        json_response(429, {}, {"Retry-After": "1"}), json_response(503, {}),
        TransportTimeout("slow"), json_response(200, [_status("test", "success")])])
    runs = provider.list_check_runs_for_sha(token="t", repository="g/p", head_sha=SHA)
    assert len(runs) == 1 and len(transport.calls) == 4
    provider, _ = _provider([json_response(500, {})] * 4)
    with pytest.raises(AppError) as caught:
        provider.list_check_runs_for_sha(token="t", repository="g/p", head_sha=SHA)
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    provider, _ = _provider([TransportTimeout("slow")] * 4)
    with pytest.raises(AppError) as caught:
        provider.list_check_runs_for_sha(token="t", repository="g/p", head_sha=SHA)
    assert caught.value.code == "PROVIDER_TIMEOUT"


def test_shared_tracker_discards_other_shas_and_requires_named_checks() -> None:
    provider, _ = _provider([json_response(200, [
        _status("test", "success"), _status("lint", "success", sha=OTHER)])])
    verification = OutcomeTracker(provider).verify_required_checks(
        token="t", repository="g/p", expected_head_sha=SHA, required_check_names=["test", "lint"])
    assert verification.mismatched_sha_discarded == 1
    assert verification.evidence_complete is False and verification.all_passed is False


# ---------------------------------------------------------------------- remotes


@pytest.mark.parametrize("url, expected", [
    ("git@gitlab.com:grp/sub/proj.git", "grp/sub/proj"),
    ("https://gitlab.com/grp/proj", "grp/proj"),
    ("https://gitlab.com/grp/proj.git/", "grp/proj"),
    ("ssh://git@gitlab.com/a/b.git", "a/b"),
    ("ssh://git@gitlab.com:22/a/b", "a/b"),
    ("https://gitlab.com/onlyone", None),
    ("https://gitlab.com/a/../b", None),
    ("https://gitlab.com/a/b.git.git", None),
    ("https://github.com/a/b", None),
    ("https://evil.example/gitlab.com/a/b", None),
    ("https://gitlab.com.evil.example/a/b", None),
    ("git@gitlab.com:a/b c", None),
    ("https://user:secret@gitlab.com/a/b", None),
])
def test_gitlab_remote_parsing(url, expected) -> None:
    assert gitlab_project_path(url) == expected


def test_project_path_is_read_from_origin(tmp_path) -> None:
    repo = make_repo(tmp_path / "repo")
    assert resolve_gitlab_project_path(str(repo)) is None
    git(repo, "remote", "add", "origin", "https://gitlab.com/team/app.git")
    assert resolve_gitlab_project_path(str(repo)) == "team/app"


# ------------------------------------------------------------------------- HTTP


def _app(tmp_path, transport):
    app = create_app(settings=Settings(database_path=tmp_path / "state" / "api.sqlite3"),
                     credential_store=InMemoryCredentialStore(), http_transport=transport)
    client = TestClient(app)
    client.__enter__()
    client.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return app, client


def _setup(client, repo, scopes):
    change = client.post("/api/v1/changes", json={
        "title": "gitlab ci", "intent": "read GitLab CI", "repository_path": str(repo)}).json()
    human = client.post("/api/v1/actors", json={"kind": "HUMAN", "display_name": "H"}).json()
    agent = client.post("/api/v1/actors", json={"kind": "AGENT", "display_name": "A"}).json()
    response = client.post("/api/v1/delegations", json={
        "grantor_id": human["id"], "grantee_id": agent["id"], "change_id": change["id"],
        "scopes": scopes, "ttl_seconds": 600})
    assert response.status_code == 201, response.text
    return change, agent


def test_refresh_reads_gitlab_statuses_for_a_gitlab_origin(tmp_path) -> None:
    repo = make_repo(tmp_path / "repo")
    git(repo, "remote", "add", "origin", "git@gitlab.com:team/app.git")
    head = git(repo, "rev-parse", "HEAD").strip()
    transport = FakeHttpTransport([json_response(200, [
        {"name": "test", "status": "success", "sha": head},
        {"name": "lint", "status": "success", "sha": head}])])
    app, client = _app(tmp_path, transport)
    try:
        change, agent = _setup(client, repo, ["gitlab.repo.read"])
        assert client.post("/api/v1/providers/gitlab/connect", json={"token": "glpat-secret"}).status_code == 200
        assert client.get("/api/v1/providers/gitlab/status").json() == {"provider": "gitlab", "configured": True}
        assert client.post(f"/api/v1/changes/{change['id']}/refresh").status_code == 200
        grant = client.post(f"/api/v1/changes/{change['id']}/providers/gitlab/grants", json={
            "actor_id": agent["id"], "scopes": ["gitlab.repo.read"], "ttl_seconds": 300})
        assert grant.status_code == 201, grant.text
        refreshed = client.post(f"/api/v1/changes/{change['id']}/outcomes/refresh", json={
            "actor_id": agent["id"], "grant_id": grant.json()["id"],
            "required_check_names": ["test", "lint"]})
        assert refreshed.status_code == 200, refreshed.text
        outcome = refreshed.json()["items"][0]
        assert outcome["status"] == "PASSED" and outcome["repository"] == "team/app"
        assert outcome["details"]["checks"] == {"test": "SUCCESS", "lint": "SUCCESS"}
        call = transport.calls[0]
        assert call["url"].startswith("https://gitlab.com/api/v4/projects/team%2Fapp/")
        assert call["headers"]["Authorization"] == "Bearer glpat-secret"
        with app.state.change_service.repository.database.connection() as connection:
            payloads = [row["payload_json"] for row in connection.execute(
                "SELECT payload_json FROM journal_events WHERE change_id = ?", (change["id"],))]
        assert any('"provider":"gitlab"' in payload.replace(" ", "") for payload in payloads)
        assert not any("glpat-secret" in payload for payload in payloads)
    finally:
        client.__exit__(None, None, None)


def test_gitlab_refresh_needs_the_gitlab_scope_and_grant_route_rejects_other_scopes(tmp_path) -> None:
    repo = make_repo(tmp_path / "repo")
    git(repo, "remote", "add", "origin", "https://gitlab.com/team/app.git")
    transport = FakeHttpTransport([])
    app, client = _app(tmp_path, transport)
    try:
        # Delegated only the GitHub read scope: a GitLab refresh is refused before any call.
        change, agent = _setup(client, repo, ["github.repo.read"])
        client.post("/api/v1/providers/gitlab/connect", json={"token": "t"})
        bad = client.post(f"/api/v1/changes/{change['id']}/providers/gitlab/grants", json={
            "actor_id": agent["id"], "scopes": ["github.repo.read"], "ttl_seconds": 300})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "GRANT_SCOPE_INVALID"
        github_grant = client.post(f"/api/v1/changes/{change['id']}/providers/github/grants", json={
            "actor_id": agent["id"], "scopes": ["github.repo.read"], "ttl_seconds": 300})
        assert github_grant.status_code == 201, github_grant.text
        refused = client.post(f"/api/v1/changes/{change['id']}/outcomes/refresh", json={
            "actor_id": agent["id"], "grant_id": github_grant.json()["id"], "required_check_names": []})
        assert refused.status_code in (403, 409), refused.text
        assert transport.calls == []
    finally:
        client.__exit__(None, None, None)


def test_github_origin_still_uses_github(tmp_path) -> None:
    repo = make_repo(tmp_path / "repo")
    git(repo, "remote", "add", "origin", "https://github.com/team/app.git")
    head = git(repo, "rev-parse", "HEAD").strip()
    transport = FakeHttpTransport([json_response(200, {"total_count": 1, "check_runs": [
        {"name": "test", "status": "completed", "conclusion": "success", "head_sha": head}]})])
    app, client = _app(tmp_path, transport)
    try:
        change, agent = _setup(client, repo, ["github.repo.read"])
        client.post("/api/v1/providers/github/connect", json={"token": "ghp"})
        client.post(f"/api/v1/changes/{change['id']}/refresh")
        grant = client.post(f"/api/v1/changes/{change['id']}/providers/github/grants", json={
            "actor_id": agent["id"], "scopes": ["github.repo.read"], "ttl_seconds": 300}).json()
        refreshed = client.post(f"/api/v1/changes/{change['id']}/outcomes/refresh", json={
            "actor_id": agent["id"], "grant_id": grant["id"], "required_check_names": ["test"]})
        assert refreshed.status_code == 200, refreshed.text
        assert transport.calls[0]["url"].startswith("https://api.github.com/repos/team/app/")
    finally:
        client.__exit__(None, None, None)
