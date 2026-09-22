import json

import pytest
from typer.testing import CliRunner

from backend.app.cli import main as cli_main
from backend.app.cli.client import ApiClient
from backend.tests.providers.fakes import FakeHttpTransport, json_response

runner = CliRunner()

CHANGE_ID = "00000000-0000-0000-0000-0000000000c1"
TASK_ID = "00000000-0000-0000-0000-0000000000a1"


def _patch_client(monkeypatch: pytest.MonkeyPatch, transport: FakeHttpTransport) -> None:
    def factory(api_url: str) -> ApiClient:
        return ApiClient(api_url, transport=transport)

    monkeypatch.setattr(cli_main, "ApiClient", factory)


def test_task_create_list_show(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeHttpTransport(
        [
            json_response(201, {"id": TASK_ID, "state": "DRAFT"}),
            json_response(200, {"items": [{"id": TASK_ID}], "count": 1, "total": 1}),
            json_response(200, {"id": TASK_ID, "state": "DRAFT"}),
        ]
    )
    _patch_client(monkeypatch, transport)

    created = runner.invoke(
        cli_main.app,
        ["task", "create", CHANGE_ID, "Do the thing", "Do it well", "claude", "--json"],
    )
    assert created.exit_code == 0, created.stdout
    assert json.loads(created.stdout)["state"] == "DRAFT"

    listed = runner.invoke(cli_main.app, ["task", "list", CHANGE_ID, "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["count"] == 1

    shown = runner.invoke(cli_main.app, ["task", "show", CHANGE_ID, TASK_ID, "--json"])
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["id"] == TASK_ID


def test_task_submit_and_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeHttpTransport(
        [
            json_response(200, {"id": TASK_ID, "state": "READY"}),
            json_response(200, {"id": TASK_ID, "state": "CANCELLED"}),
        ]
    )
    _patch_client(monkeypatch, transport)

    submitted = runner.invoke(
        cli_main.app,
        ["task", "submit", CHANGE_ID, TASK_ID, "--expected-revision", "1", "--json"],
    )
    assert submitted.exit_code == 0
    assert json.loads(submitted.stdout)["state"] == "READY"

    cancelled = runner.invoke(
        cli_main.app,
        [
            "task", "cancel", CHANGE_ID, TASK_ID,
            "--expected-revision", "2", "--reason", "not needed", "--json",
        ],
    )
    assert cancelled.exit_code == 0
    assert json.loads(cancelled.stdout)["state"] == "CANCELLED"


def test_task_dependency_cycle_error_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeHttpTransport(
        [
            json_response(
                409,
                {
                    "error": {
                        "code": "TASK_DEPENDENCY_CYCLE",
                        "message": "cycle",
                        "details": {"cycle": [TASK_ID]},
                    }
                },
            )
        ]
    )
    _patch_client(monkeypatch, transport)

    result = runner.invoke(
        cli_main.app,
        [
            "task", "dependencies", CHANGE_ID, TASK_ID,
            "--expected-revision", "1", "--depends-on", TASK_ID, "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "TASK_DEPENDENCY_CYCLE"
