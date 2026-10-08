"""sentinel doctor reports each prerequisite with the same probes Sentinel uses."""

from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from backend.app.cli import doctor
from backend.app.cli.main import app


def test_doctor_json_lists_every_check_and_exits_by_failures() -> None:
    result = CliRunner().invoke(app, ["doctor", "--json"])
    checks = {item["name"]: item for item in json.loads(result.output)["checks"]}
    assert {"python", "git", "store", "credentials"} <= set(checks)
    assert checks["python"]["status"] == "ok"
    failed = any(item["status"] == "fail" for item in checks.values())
    assert result.exit_code == (1 if failed else 0)
    if not sys.platform.startswith("linux"):
        assert checks["linux_sandbox"]["status"] == "skip"


def test_a_missing_bwrap_is_a_failure_with_its_remedy(monkeypatch) -> None:
    from backend.app.core.errors import AppError
    from backend.app.execution import linux_sandbox

    def missing(environ=None):
        raise AppError("LINUX_SANDBOX_UNAVAILABLE", "bubblewrap (bwrap) is not installed")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(linux_sandbox, "find_bwrap", missing)
    [check] = doctor._linux_sandbox()
    assert check.status == "fail" and "bubblewrap" in check.remedy
    assert doctor.exit_code([check]) == 1
