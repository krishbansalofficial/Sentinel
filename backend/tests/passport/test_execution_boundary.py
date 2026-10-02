"""The Passport execution boundary comes only from verified, consistent launch records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.contracts.models import JournalEventType, WorkspaceState
from backend.app.core.journal import JournalWriter
from backend.app.execution.launcher import appcontainer_authority
from backend.app.passport.boundary import change_boundary, launch_boundary
from backend.app.passport.v2 import PassportV2Issuer
from backend.app.workspace.models import WorkspaceRecord
from backend.app.workspace.repository import WorkspaceRepository
from backend.tests.passport.test_builder import _database, _seed_change

PACKAGE_SID = "S-1-15-2-1-2-3-4-5-6-7"


def _facts(**overrides: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "profile_name": "sentinel.ws.test", "package_sid": PACKAGE_SID,
        "is_appcontainer": True, "integrity_rid": "0x1000",
        "capability_sids": [], "job_verified": True,
        "verified_at": datetime.now(UTC).isoformat(),
    }
    facts.update(overrides)
    return facts


def _appcontainer_launch(facts: dict[str, object], **overrides: object) -> dict[str, object]:
    launch: dict[str, object] = {
        "status": "PASSED", "top_level_pid": 4242, "restricted_token_applied": False,
        "authority_reduction": appcontainer_authority(SimpleNamespace(**facts)),
    }
    launch.update(overrides)
    return launch


def test_verified_workspace_run_is_appcontainer() -> None:
    facts = _facts()
    run_id = uuid4()
    observed = launch_boundary(run_id, _appcontainer_launch(facts),
                               [(PACKAGE_SID, {"run_id": str(run_id), "facts": facts})])
    assert observed.boundary == "APPCONTAINER" and observed.package_sid == PACKAGE_SID
    assert change_boundary([observed]) == ("APPCONTAINER", None)


@pytest.mark.parametrize("facts, workspace_sid, launch_overrides", [
    (_facts(job_verified=False), PACKAGE_SID, {}),
    (_facts(is_appcontainer=False), PACKAGE_SID, {}),
    (_facts(integrity_rid="0x2000"), PACKAGE_SID, {}),
    (_facts(), "S-1-15-2-9-9-9", {}),  # facts name another workspace's profile
    (_facts(), PACKAGE_SID, {"authority_reduction": "edited authority text"}),
    (None, PACKAGE_SID, {}),
])
def test_unverified_or_inconsistent_run_is_never_appcontainer(
        facts: dict[str, object] | None, workspace_sid: str,
        launch_overrides: dict[str, object]) -> None:
    run_id = uuid4()
    launch = _appcontainer_launch(facts or _facts(), **launch_overrides)
    observed = launch_boundary(run_id, launch,
                               [(workspace_sid, {"run_id": str(run_id), "facts": facts})])
    assert observed.boundary == "UNKNOWN" and observed.reason


def test_appcontainer_authority_without_workspace_run_is_unknown() -> None:
    observed = launch_boundary(uuid4(), _appcontainer_launch(_facts()), [])
    assert observed.boundary == "UNKNOWN"


@pytest.mark.parametrize("launch, expected", [
    ({"status": "PASSED", "top_level_pid": 1, "restricted_token_applied": True},
     "RESTRICTED_TOKEN"),
    ({"status": "PASSED", "top_level_pid": 1, "restricted_token_applied": False}, "UNCONFINED"),
    ({"status": "PASSED", "top_level_pid": 1}, "UNKNOWN"),  # no token facts recorded
    ({"status": "ATTACHED", "restricted_token_applied": False}, "UNKNOWN"),
])
def test_launches_without_a_workspace_run(launch: dict[str, object], expected: str) -> None:
    assert launch_boundary(uuid4(), launch, []).boundary == expected


def test_change_claim_is_the_weakest_launch() -> None:
    facts = _facts()
    run_id = uuid4()
    boxed = launch_boundary(run_id, _appcontainer_launch(facts),
                            [(PACKAGE_SID, {"run_id": str(run_id), "facts": facts})])
    restricted = launch_boundary(uuid4(), {"status": "PASSED", "top_level_pid": 1,
                                           "restricted_token_applied": True}, [])
    unconfined = launch_boundary(uuid4(), {"status": "PASSED", "top_level_pid": 1,
                                           "restricted_token_applied": False}, [])
    never_started = launch_boundary(uuid4(), {"status": "ERROR", "top_level_pid": None,
                                              "restricted_token_applied": False}, [])
    assert change_boundary([boxed, restricted])[0] == "RESTRICTED_TOKEN"
    assert change_boundary([boxed, restricted, unconfined])[0] == "UNCONFINED"
    # A launch that never spawned a process ran no agent code.
    assert change_boundary([boxed, never_started])[0] == "APPCONTAINER"
    assert change_boundary([never_started])[0] == "UNKNOWN"
    assert change_boundary([])[0] == "UNKNOWN"


def _seed_appcontainer_launch(database, change_id, *, facts, workspace_sid=PACKAGE_SID):
    run_id = uuid4()
    now = datetime.now(UTC)
    payload = {"id": str(run_id), "change_id": str(change_id), "adapter": "claude",
               "status": "PASSED", **_appcontainer_launch(facts)}
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO agent_runs (id, change_id, status, payload_json, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(run_id), str(change_id), "PASSED", json.dumps(payload), now.isoformat()))
    journal = JournalWriter(database)
    journal.append(change_id, JournalEventType.AGENT_LAUNCHED, subject_type="agent_run",
                   subject_id=run_id, payload={"adapter": "claude", "executable": "claude"})
    journal.append(change_id, JournalEventType.AGENT_COMPLETED, subject_type="agent_run",
                   subject_id=run_id, payload={"status": "PASSED", "exit_code": 0})
    WorkspaceRepository(database).insert(WorkspaceRecord(
        id=uuid4(), change_id=change_id, state=WorkspaceState.CLEANED,
        profile_name="sentinel.ws.test", created_at=now, updated_at=now,
        package_sid=workspace_sid,
        runs=({"run_id": str(run_id), "status": "PASSED", "facts": facts,
               "limitations": [], "finished_at": now.isoformat()},)))
    return run_id


def test_snapshot_binds_verified_appcontainer_launch(tmp_path) -> None:
    database = _database(tmp_path)
    change = _seed_change(database)
    run_id = _seed_appcontainer_launch(database, change.id, facts=_facts())
    snapshot = PassportV2Issuer(database).snapshot(change.id)
    assert snapshot.execution_boundary == "APPCONTAINER"
    assert [(item.run_id, item.boundary, item.package_sid)
            for item in snapshot.launch_boundaries] == [(run_id, "APPCONTAINER", PACKAGE_SID)]
    assert not any("Execution boundary" in line for line in snapshot.limitations)


def test_snapshot_refuses_appcontainer_for_a_foreign_profile(tmp_path) -> None:
    database = _database(tmp_path)
    change = _seed_change(database)
    _seed_appcontainer_launch(database, change.id, facts=_facts(),
                              workspace_sid="S-1-15-2-9-9-9")
    snapshot = PassportV2Issuer(database).snapshot(change.id)
    assert snapshot.execution_boundary == "UNKNOWN"
    assert snapshot.launch_boundaries[0].boundary == "UNKNOWN"
    assert any(line.startswith("Execution boundary UNKNOWN:") for line in snapshot.limitations)
