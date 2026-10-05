"""Capabilities and /health report what this platform cannot provide."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.contracts.models import CapabilityState
from backend.app.core import capabilities as capabilities_module
from backend.app.core.capabilities import build_capabilities, unsupported_on_platform

ALL = {"agent_launcher", "process_supervisor", "credential_broker", "change_lifecycle",
       "linux_sandbox"}


@pytest.fixture
def sandbox_probe(monkeypatch):
    """Control the Linux sandbox probe instead of probing this host."""
    state = {"problem": None}

    def fake(platform: str) -> str | None:
        if not platform.startswith("linux"):
            return "The Linux sandbox exists only on Linux."
        return state["problem"]

    monkeypatch.setattr(capabilities_module, "_linux_sandbox_problem", fake)
    return state


def _by_id(platform: str) -> dict:
    return {item.id: item for item in build_capabilities(ALL, platform=platform).items}


def test_windows_reports_job_objects_and_no_linux_sandbox(sandbox_probe) -> None:
    items = _by_id("win32")
    assert items["process_supervisor"].state is CapabilityState.AVAILABLE
    assert items["agent_launcher"].state is CapabilityState.AVAILABLE
    assert len(items["agent_launcher"].limitations) == 1
    assert items["linux_sandbox"].state is CapabilityState.UNSUPPORTED
    assert set(unsupported_on_platform("win32")) == {"linux_sandbox"}


@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14"])
def test_off_windows_job_objects_are_unsupported_even_when_configured(
        sandbox_probe, platform: str) -> None:
    items = _by_id(platform)
    supervisor = items["process_supervisor"]
    assert supervisor.state is CapabilityState.UNSUPPORTED
    assert "Job Objects" in supervisor.reason
    launcher = items["agent_launcher"]
    assert launcher.state is CapabilityState.AVAILABLE
    assert any("fail" in text and "closed" in text for text in launcher.limitations)
    assert items["change_lifecycle"].state is CapabilityState.AVAILABLE


def test_linux_with_a_working_sandbox_reports_it_available(sandbox_probe) -> None:
    sandbox_probe["problem"] = None
    items = _by_id("linux")
    assert items["linux_sandbox"].state is CapabilityState.AVAILABLE
    assert set(unsupported_on_platform("linux")) == {"process_supervisor"}


def test_linux_without_the_prerequisites_says_why(sandbox_probe) -> None:
    sandbox_probe["problem"] = "bubblewrap (bwrap) is not installed"
    items = _by_id("linux")
    assert items["linux_sandbox"].state is CapabilityState.UNSUPPORTED
    assert items["linux_sandbox"].reason == "bubblewrap (bwrap) is not installed"
    assert set(unsupported_on_platform("linux")) == {"process_supervisor", "linux_sandbox"}


def test_darwin_has_neither_boundary(sandbox_probe) -> None:
    assert set(unsupported_on_platform("darwin")) == {"process_supervisor", "linux_sandbox"}


def test_unconfigured_stays_unconfigured_and_unsupported_wins(sandbox_probe) -> None:
    items = {item.id: item for item in build_capabilities(set(), platform="linux").items}
    assert items["change_lifecycle"].state is CapabilityState.UNCONFIGURED
    assert items["process_supervisor"].state is CapabilityState.UNSUPPORTED


def test_the_real_probe_never_mutates_and_is_cached(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(capabilities_module, "_PROBE_CACHE", {})
    import backend.app.execution.linux_sandbox as linux_sandbox

    def fake_require(environ=None):
        calls.append("require")
        from backend.app.core.errors import AppError
        raise AppError("LINUX_SANDBOX_UNAVAILABLE", "no bwrap here")

    monkeypatch.setattr(linux_sandbox, "require_sandbox", fake_require)
    assert capabilities_module._linux_sandbox_problem("linux") == "no bwrap here"
    assert capabilities_module._linux_sandbox_problem("linux") == "no bwrap here"
    assert calls == ["require"]  # cached for the TTL


def test_health_and_capabilities_routes_report_this_platform(tmp_path: Path, sandbox_probe) -> None:
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
    assert "linux_sandbox" in listed


@pytest.mark.skipif(sys.platform == "win32" or sys.platform.startswith("linux"),
                    reason="the platform with no declared claude boundary (macOS and others)")
def test_claude_launch_fails_closed_without_a_declared_boundary(tmp_path: Path) -> None:
    from backend.app.contracts.models import AgentLaunchRequest
    from backend.app.core.errors import AppError
    from backend.app.execution.launcher import AgentLauncher

    with pytest.raises(AppError) as raised:
        AgentLauncher().launch(uuid4(), str(tmp_path),
                               AgentLaunchRequest(adapter="claude", executable="claude"), 65536)
    assert raised.value.code == "AGENT_RUNTIME_PROFILE_UNAVAILABLE"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the Linux refusal path")
def test_claude_launch_on_linux_needs_a_workspace_provider(tmp_path: Path) -> None:
    from backend.app.contracts.models import AgentLaunchRequest
    from backend.app.core.errors import AppError
    from backend.app.execution.launcher import AgentLauncher

    with pytest.raises(AppError) as raised:
        AgentLauncher().launch(uuid4(), str(tmp_path),
                               AgentLaunchRequest(adapter="claude", executable="claude"), 65536)
    assert raised.value.code == "AGENT_WORKSPACE_UNAVAILABLE"
