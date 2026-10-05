"""A sandboxed launch end to end: launcher, workspace clone, evidence, Passport claim."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.app.assurance.service import EvidenceService
from backend.app.assurance.store import EvidenceStore
from backend.app.contracts.models import AgentLaunchRequest, AgentRunStatus
from backend.app.core.journal import JournalWriter
from backend.app.execution.agent_profiles import BoundaryKind, RuntimeProfile
from backend.app.execution.launcher import (
    LINUX_SUPERVISED_LIMITATION,
    AgentAdapter,
    AgentLauncher,
    linux_sandbox_authority,
)
from backend.app.passport.v2 import PassportV2Issuer
from backend.app.workspace.manager import WorkspaceManager
from backend.app.workspace.profiles import LinuxWorkspaceProfiles
from backend.tests.execution.linux.conftest import requires_linux_sandbox
from backend.tests.passport.test_builder import _database, _seed_change
from backend.tests.support_kb import make_repo

pytestmark = requires_linux_sandbox
PYTHON_NAME = Path(os.path.realpath(sys.executable)).name


def _setup(tmp_path: Path, hierarchy_factory, *, network: bool = False):
    repo = make_repo(tmp_path / "repo", {"app.py": "VALUE = 1\n"})
    database = _database(tmp_path)
    change = _seed_change(database)
    with database.connection() as connection:
        connection.execute("UPDATE changes SET repository_path = ? WHERE id = ?",
                           (str(repo), str(change.id)))
    change = change.model_copy(update={"repository_path": str(repo)})
    journal = JournalWriter(database)
    manager = WorkspaceManager(database, journal=journal,
                               profiles=LinuxWorkspaceProfiles(tmp_path / "store"))
    profile = RuntimeProfile(
        "sandboxed", BoundaryKind.LINUX_SANDBOX, tool_snapshot=True, staged_home=True,
        capabilities=("internetClient",) if network else ())
    launcher = AgentLauncher(
        adapters={"sandboxed": AgentAdapter("sandboxed", frozenset({PYTHON_NAME}))},
        profiles={"sandboxed": profile}, workspaces=manager, cgroups=hierarchy_factory)
    evidence = EvidenceService(EvidenceStore(database), launcher=launcher, journal=journal)
    return repo, database, change, manager, launcher, evidence


def test_a_sandboxed_run_is_bound_to_a_linux_sandbox_passport_claim(tmp_path, hierarchy_factory) -> None:
    repo, database, change, manager, _launcher, evidence = _setup(tmp_path, hierarchy_factory)
    code = ("import os, pathlib; pathlib.Path('agent.txt').write_text('edited');"
            "print(os.getcwd() != %r, os.environ['HOME'])" % str(repo))
    run = evidence.launch_agent(change, AgentLaunchRequest(
        adapter="sandboxed", executable=PYTHON_NAME, args=["-c", code], timeout_seconds=60))
    assert run.status is AgentRunStatus.PASSED, (run.stderr, run.limitations)
    assert run.stdout.split()[0] == "True"  # it ran in the workspace clone, not the repo
    assert not (repo / "agent.txt").exists()
    assert run.authority_reduction and run.authority_reduction.startswith("Linux sandbox verified")
    assert run.descendant_control_available
    assert LINUX_SUPERVISED_LIMITATION in run.limitations
    workspace = manager.repository.live_for_change(change.id)
    assert workspace.package_sid.startswith("linux-sandbox:")
    assert (Path(workspace.workspace_path) / "agent.txt").read_text() == "edited"
    facts = workspace.runs[-1]["facts"]
    assert linux_sandbox_authority(facts) == run.authority_reduction

    payload = PassportV2Issuer(database).snapshot(change.id)
    assert payload.execution_boundary == "LINUX_SANDBOX"
    assert [item.boundary for item in payload.launch_boundaries] == ["LINUX_SANDBOX"]
    assert payload.launch_boundaries[0].package_sid == workspace.package_sid


def test_tampered_facts_drop_the_claim_to_unknown(tmp_path, hierarchy_factory) -> None:
    _repo, database, change, manager, _launcher, evidence = _setup(tmp_path, hierarchy_factory)
    evidence.launch_agent(change, AgentLaunchRequest(
        adapter="sandboxed", executable=PYTHON_NAME, args=["-c", "pass"], timeout_seconds=60))
    workspace = manager.repository.live_for_change(change.id)
    import json
    with database.connection() as connection:
        row = connection.execute("SELECT payload_json FROM change_workspaces WHERE id = ?",
                                 (str(workspace.id),)).fetchone()
        payload = json.loads(row[0])
        facts = payload["runs"][-1]["facts"]
        facts["seccomp_filters"] = facts["supervisor_seccomp_filters"]  # "no filter added"
        connection.execute("UPDATE change_workspaces SET payload_json = ? WHERE id = ?",
                           (json.dumps(payload), str(workspace.id)))
    payload = PassportV2Issuer(database).snapshot(change.id)
    assert payload.execution_boundary == "UNKNOWN"


def test_stop_kills_the_whole_sandboxed_tree(tmp_path, hierarchy_factory) -> None:
    _repo, _database_, change, _manager, launcher, evidence = _setup(tmp_path, hierarchy_factory)
    code = ("import os, time\nif os.fork() == 0:\n    os.setsid()\n    time.sleep(600)\n"
            "time.sleep(600)\n")
    holder: dict[str, object] = {}
    thread = threading.Thread(target=lambda: holder.setdefault("run", evidence.launch_agent(
        change, AgentLaunchRequest(adapter="sandboxed", executable=PYTHON_NAME,
                                   args=["-c", code], timeout_seconds=120))))
    thread.start()
    deadline = time.monotonic() + 20
    active = None
    while time.monotonic() < deadline:
        with launcher._lock:
            states = list(launcher._runs.values())
        active = states[0] if states else None
        if active and active.record.descendant_processes:
            break
        time.sleep(0.05)
    assert active is not None and active.record.descendant_processes
    cgroup = active.process.session.cgroup
    launcher.pause(active.record.id)
    assert cgroup.frozen()
    launcher.resume(active.record.id)
    assert not cgroup.frozen()
    launcher.stop(active.record.id)
    thread.join(30)
    run = holder["run"]
    assert run.status is AgentRunStatus.CANCELLED
    assert "Cancellation terminated the supervised process tree." in run.limitations
    assert not cgroup.path.exists()


def test_a_script_shim_is_refused_for_a_native_only_profile(tmp_path, hierarchy_factory) -> None:
    _repo, _db, change, _manager, _launcher, _evidence = _setup(tmp_path, hierarchy_factory)
    from backend.app.core.errors import AppError
    shim = tmp_path / "bin" / "agentshim"
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\nexec python3 \"$@\"\n")
    shim.chmod(0o755)
    profile = RuntimeProfile("native", BoundaryKind.LINUX_SANDBOX, requires_native_executable=True)
    launcher = AgentLauncher(adapters={"native": AgentAdapter("native", frozenset({"agentshim"}))},
                             profiles={"native": profile}, workspaces=_manager,
                             cgroups=hierarchy_factory)
    os.environ["PATH"] = f"{shim.parent}:{os.environ['PATH']}"
    with pytest.raises(AppError) as raised:
        launcher.launch(change.id, str(_repo), AgentLaunchRequest(
            adapter="native", executable="agentshim"), 1000)
    assert raised.value.code == "AGENT_RUNTIME_PROFILE_UNAVAILABLE"
