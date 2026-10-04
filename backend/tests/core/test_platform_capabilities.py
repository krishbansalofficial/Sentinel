"""Capabilities and /health report what this platform cannot provide."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.contracts.models import CapabilityState
from backend.app.core.capabilities import build_capabilities, unsupported_on_platform

ALL = {"agent_launcher", "process_supervisor", "credential_broker", "change_lifecycle"}


def _by_id(platform: str) -> dict:
    return {item.id: item for item in build_capabilities(ALL, platform=platform).items}


def test_windows_reports_everything_configured_as_available() -> None:
    items = _by_id("win32")
    assert items["process_supervisor"].state is CapabilityState.AVAILABLE
    assert items["agent_launcher"].state is CapabilityState.AVAILABLE
    assert len(items["agent_launcher"].limitations) == 1
    assert unsupported_on_platform("win32") == {}


@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14"])
def test_off_windows_job_objects_are_unsupported_even_when_configured(platform: str) -> None:
    items = _by_id(platform)
    supervisor = items["process_supervisor"]
    assert supervisor.state is CapabilityState.UNSUPPORTED
    assert "Job Objects" in supervisor.reason
    launcher = items["agent_launcher"]
    assert launcher.state is CapabilityState.AVAILABLE
    assert any("fail closed" in text for text in launcher.limitations)
    assert items["change_lifecycle"].state is CapabilityState.AVAILABLE
    assert set(unsupported_on_platform(platform)) == {"process_supervisor"}


def test_unconfigured_stays_unconfigured_and_unsupported_wins() -> None:
    items = {item.id: item for item in build_capabilities(set(), platform="linux").items}
    assert items["change_lifecycle"].state is CapabilityState.UNCONFIGURED
    assert items["process_supervisor"].state is CapabilityState.UNSUPPORTED


def test_health_and_capabilities_routes_report_this_platform(tmp_path: Path) -> None:
    from backend.app.core.config import Settings
    from backend.app.credentials.memory_store import InMemoryCredentialStore
    from backend.app.main import create_app

    app = create_app(settings=Settings(database_path=tmp_path / "s" / "db.sqlite3"),
                     credential_store=InMemoryCredentialStore())
    with TestClient(app) as client:
        health = client.get("/api/v1/health").json()  # unauthenticated by design
        assert health["platform"] == sys.platform
        assert health["unsupported_capabilities"] == sorted(unsupported_on_platform())
        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        listed = {item["id"]: item for item in client.get("/api/v1/capabilities").json()["items"]}
    expected = "AVAILABLE" if sys.platform == "win32" else "UNSUPPORTED"
    assert listed["process_supervisor"]["state"] == expected


@pytest.mark.skipif(sys.platform == "win32", reason="the off-Windows refusal path")
def test_claude_launch_fails_closed_off_windows(tmp_path: Path) -> None:
    from backend.app.contracts.models import AgentLaunchRequest
    from backend.app.core.errors import AppError
    from backend.app.execution.launcher import AgentLauncher

    launcher = AgentLauncher()
    with pytest.raises(AppError) as raised:
        launcher.launch(uuid4(), str(tmp_path),
                        AgentLaunchRequest(adapter="claude", executable="claude"), 65536)
    assert raised.value.code == "APPCONTAINER_UNSUPPORTED"
