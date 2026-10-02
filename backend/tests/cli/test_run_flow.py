"""`sentinel run` chains the workflow and stops at every gate without weakening apply approval."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

import backend.app.cli.main as cli_main
from backend.app.cli.client import ApiError
from backend.app.cli.run_flow import (
    EXIT_GATE_STOPPED, FAILED, OK, SKIPPED, STOPPED, RunFlow, RunOptions,
)

CHANGE, ACTOR = uuid4(), uuid4()
TOKEN = "approval-token-value"


class FakeClient:
    """Records calls; each behavior is configurable per test."""

    def __init__(self, *, baseline_exists=False, agent_status="PASSED", preview=None,
                 workspace=True, applied=True, fail_on=None):
        self.calls: list[str] = []
        self.baseline_exists = baseline_exists
        self.agent_status = agent_status
        self.preview = preview if preview is not None else {
            "approval_token": TOKEN, "refusal_reason": None, "fast_forward_possible": True,
            "changed_paths": [{"path": "calc.py"}, {"path": "notes.txt"}]}
        self.workspace = workspace
        self.applied = applied
        self.fail_on = fail_on
        self.apply_body = None

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name == self.fail_on:
            raise ApiError("BOOM", f"{name} failed", status_code=500)

    def capture_baseline(self, change_id):
        self._maybe_fail("baseline")
        if self.baseline_exists:
            raise ApiError("BASELINE_EXISTS", "exists", status_code=409)
        return {"checkpoint": {"head_sha": "a" * 40}}

    def launch_agent(self, change_id, **kwargs):
        self._maybe_fail("launch")
        self.launch_kwargs = kwargs
        return {"id": str(uuid4()), "status": self.agent_status, "exit_code": 0}

    def capture_current_evidence(self, change_id):
        self._maybe_fail("current")
        return {"checkpoint": {"head_sha": "b" * 40}}

    def _request(self, method, path, json_body=None, **_):
        if path.endswith("/preview"):
            self._maybe_fail("preview")
            if not self.workspace:
                raise ApiError("WORKSPACE_NOT_FOUND", "none", status_code=404)
            return self.preview
        if path.endswith("/apply"):
            self._maybe_fail("apply")
            self.apply_body = json_body
            return {"applied": self.applied,
                    "workspace": {"applied_sha": "c" * 40 if self.applied else None,
                                  "refusal_reason": None if self.applied else "USER_BRANCH_MOVED"}}
        if path.endswith("/passport/v2/issue"):
            self._maybe_fail("passport")
            return {"payload_digest": "d" * 64, "signer_fingerprint": "FP",
                    "payload": {"execution_boundary": "APPCONTAINER", "policy_decision": "ALLOW"}}
        raise AssertionError(f"unexpected request {method} {path}")


def _run(client, *, confirm=None, **options):
    return RunFlow(client, confirm=confirm).run(
        CHANGE, ACTOR, RunOptions(executable="claude", args=["-p", "fix"], **options))


def _statuses(report):
    return [(step.name, step.status) for step in report.steps]


def test_default_stops_after_preview_with_the_apply_command() -> None:
    client = FakeClient()
    report = _run(client)
    assert _statuses(report) == [("baseline", OK), ("launch", OK), ("preview", OK),
                                 ("apply", STOPPED)]
    apply_step = report.steps[-1].as_dict()
    assert apply_step["approval_token"] == TOKEN
    assert apply_step["command"].startswith(f"sentinel workspace apply {CHANGE} {ACTOR}")
    assert "apply" not in client.calls and report.outcome == STOPPED


def test_confirmed_apply_runs_the_whole_flow() -> None:
    client = FakeClient()
    seen = []
    report = _run(client, confirm=lambda preview: seen.append(preview) or True,
                  apply=True, issue_passport=True)
    assert report.outcome == OK
    assert _statuses(report) == [("baseline", OK), ("launch", OK), ("preview", OK),
                                 ("apply", OK), ("current_evidence", OK), ("passport", OK)]
    assert client.apply_body == {"actor_id": str(ACTOR), "approval_token": TOKEN}
    assert seen and seen[0]["approval_token"] == TOKEN
    assert report.steps[-1].detail["execution_boundary"] == "APPCONTAINER"


def test_apply_without_a_confirmer_is_never_confirmed() -> None:
    client = FakeClient()
    report = _run(client, apply=True)
    assert report.steps[-1].name == "apply" and report.steps[-1].status == STOPPED
    assert "apply" not in client.calls


def test_declined_confirmation_does_not_apply() -> None:
    client = FakeClient()
    report = _run(client, confirm=lambda _preview: False, apply=True)
    assert report.steps[-1].detail["reason"] == "apply was not confirmed"
    assert "apply" not in client.calls


def test_existing_baseline_is_reused_not_replaced() -> None:
    report = _run(FakeClient(baseline_exists=True))
    assert report.steps[0].as_dict() == {"step": "baseline", "status": OK, "reused": True}


@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "ERROR", "CANCELLED"])
def test_unsuccessful_agent_run_stops_before_preview(status) -> None:
    client = FakeClient(agent_status=status)
    report = _run(client, confirm=lambda _p: True, apply=True)
    assert _statuses(report)[-1] == ("launch", STOPPED)
    assert "preview" not in client.calls and "apply" not in client.calls


@pytest.mark.parametrize("preview", [
    {"approval_token": None, "refusal_reason": "CREDENTIAL_IN_DIFF", "changed_paths": []},
    {"approval_token": None, "refusal_reason": "FORBIDDEN_PATH_IN_DIFF", "changed_paths": []},
    {"approval_token": None, "refusal_reason": None, "changed_paths": []},
])
def test_refused_preview_stops_and_never_applies(preview) -> None:
    client = FakeClient(preview=preview)
    report = _run(client, confirm=lambda _p: True, apply=True)
    assert _statuses(report)[-1] == ("preview", STOPPED)
    assert "apply" not in client.calls


def test_server_refused_apply_is_a_stop() -> None:
    client = FakeClient(applied=False)
    report = _run(client, confirm=lambda _p: True, apply=True)
    assert report.steps[-1].as_dict()["refusal_reason"] == "USER_BRANCH_MOVED"
    assert report.outcome == STOPPED and "current" not in client.calls


def test_generic_adapter_without_workspace_skips_preview_and_apply() -> None:
    client = FakeClient(workspace=False)
    report = _run(client, confirm=lambda _p: True, apply=True)
    assert _statuses(report) == [("baseline", OK), ("launch", OK), ("preview", SKIPPED),
                                 ("apply", SKIPPED), ("current_evidence", OK),
                                 ("passport", SKIPPED)]
    assert report.outcome == OK and "apply" not in client.calls


@pytest.mark.parametrize("step", ["baseline", "launch", "preview", "apply", "current", "passport"])
def test_api_error_at_any_step_fails_and_stops(step) -> None:
    client = FakeClient(fail_on=step)
    report = _run(client, confirm=lambda _p: True, apply=True, issue_passport=True)
    assert report.outcome == FAILED
    assert report.steps[-1].status == FAILED
    assert report.steps[-1].detail["error"]["code"] == "BOOM"
    assert client.calls[-1] == step  # nothing ran after the failure


def test_launch_options_are_passed_through() -> None:
    client = FakeClient()
    RunFlow(client).run(CHANGE, ACTOR, RunOptions(
        executable="node", args=["agent.js"], adapter="claude", environment_keys=["HOME"],
        timeout_seconds=30, output_limit_bytes=1024))
    assert client.launch_kwargs == {
        "actor_id": ACTOR, "executable": "node", "args": ["agent.js"], "adapter": "claude",
        "environment_keys": ["HOME"], "timeout_seconds": 30, "output_limit_bytes": 1024}


def test_report_never_contains_the_full_preview() -> None:
    report = _run(FakeClient(preview={
        "approval_token": TOKEN, "refusal_reason": None, "changed_paths": [{"path": "a"}],
        "patch": "SECRET PATCH BODY"}))
    assert "SECRET PATCH BODY" not in json.dumps(report.as_dict())


# ------------------------------------------------------------------------------ CLI


@pytest.fixture
def fake_cli(monkeypatch):
    holder = {}

    def factory(_api_url):
        return holder["client"]
    monkeypatch.setattr(cli_main, "ApiClient", factory)
    return holder


def _invoke(*extra, input=None):
    return CliRunner().invoke(cli_main.app, ["run", str(CHANGE), str(ACTOR), "claude", *extra],
                              input=input)


def test_cli_json_reports_stop_with_exit_code(fake_cli) -> None:
    fake_cli["client"] = FakeClient()
    result = _invoke("--json")
    assert result.exit_code == EXIT_GATE_STOPPED
    summary = json.loads(result.stdout)
    assert summary["outcome"] == STOPPED and summary["steps"][-1]["step"] == "apply"


def test_cli_json_with_apply_never_prompts_or_applies(fake_cli) -> None:
    client = fake_cli["client"] = FakeClient()
    result = _invoke("--json", "--apply")
    assert result.exit_code == EXIT_GATE_STOPPED
    assert "apply" not in client.calls


def test_cli_prompt_confirms_or_declines(fake_cli) -> None:
    client = fake_cli["client"] = FakeClient()
    declined = _invoke("--apply", input="n\n")
    assert declined.exit_code == EXIT_GATE_STOPPED and "apply" not in client.calls
    assert "calc.py" in declined.stdout
    client = fake_cli["client"] = FakeClient()
    accepted = _invoke("--apply", input="y\n")
    assert accepted.exit_code == 0, accepted.stdout
    assert "apply" in client.calls


def test_cli_yes_requires_apply_and_confirms(fake_cli) -> None:
    fake_cli["client"] = FakeClient()
    assert _invoke("--yes").exit_code != 0
    client = fake_cli["client"] = FakeClient()
    assert _invoke("--apply", "--yes", "--json").exit_code == 0
    assert "apply" in client.calls


def test_cli_failed_step_exits_with_api_error(fake_cli) -> None:
    fake_cli["client"] = FakeClient(fail_on="launch")
    result = _invoke("--json")
    assert result.exit_code == cli_main.EXIT_API_ERROR
    assert json.loads(result.stdout)["outcome"] == FAILED


# ------------------------------------------------------------------- real server


def test_generic_run_against_a_real_server(tmp_path, monkeypatch) -> None:
    """End to end over HTTP: generic adapter, real Git repository, real journal."""
    from fastapi.testclient import TestClient

    from backend.app.core.config import Settings
    from backend.app.credentials.memory_store import InMemoryCredentialStore
    from backend.app.main import create_app
    from backend.tests.support_kb import make_repo

    repo = make_repo(tmp_path / "repo", {"calc.py": "def add(a, b):\n    return a + b\n"})
    # "python" must resolve to this real interpreter (as on CI), not a Store alias stub.
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep
                       + os.environ.get("PATH", ""))
    app = create_app(settings=Settings(database_path=tmp_path / "state" / "api.sqlite3"),
                     credential_store=InMemoryCredentialStore())
    with TestClient(app) as http:
        http.headers["Authorization"] = f"Bearer {app.state.api_token}"

        from backend.app.cli.client import ApiClient
        from backend.app.providers.http_transport import HttpResponse

        class InProcessTransport:
            """The real ApiClient's requests, served by the in-process app."""

            def request(self, method, url, *, headers, body, **_):
                path = url.split("://", 1)[1].split("/", 1)[1]
                response = http.request(method, "/" + path, headers=dict(headers),
                                        content=body)
                return HttpResponse(status_code=response.status_code,
                                    headers=dict(response.headers), body=response.content)

        client = ApiClient("http://testserver", transport=InProcessTransport(),
                           token=app.state.api_token)

        change = http.post("/api/v1/changes", json={
            "title": "run", "intent": "one-shot flow", "repository_path": str(repo)}).json()
        human = http.post("/api/v1/actors", json={"kind": "HUMAN", "display_name": "H"}).json()
        agent = http.post("/api/v1/actors", json={"kind": "AGENT", "display_name": "A"}).json()
        delegation = http.post("/api/v1/delegations", json={
            "grantor_id": human["id"], "grantee_id": agent["id"], "change_id": change["id"],
            "scopes": ["agent.launch"], "ttl_seconds": 600})
        assert delegation.status_code == 201, delegation.text
        report = RunFlow(client).run(UUID(change["id"]), UUID(agent["id"]), RunOptions(
            executable="python", args=["-c", "print('agent ran')"],
            timeout_seconds=60))
    statuses = _statuses(report)
    assert statuses[:2] == [("baseline", OK), ("launch", OK)], report.as_dict()
    assert ("preview", SKIPPED) in statuses and ("current_evidence", OK) in statuses
    assert report.outcome == OK, report.as_dict()
