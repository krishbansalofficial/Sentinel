"""Seeded mock-data stress over every feature added on 2026-10-02/03: invariants, not examples."""

from __future__ import annotations

import json
import random
from uuid import uuid4

from backend.app.assurance.v8_coverage import line_coverage
from backend.app.cli.run_flow import RunFlow, RunOptions
from backend.app.contracts.models import ChangeContract
from backend.app.outcomes.tracker import OutcomeTracker
from backend.app.passport.boundary import change_boundary, launch_boundary
from backend.app.passport.v2 import PassportV2Issuer
from backend.app.policy.gate import preset_allows
from backend.app.providers.gitlab import GitLabProvider
from backend.tests.cli.test_run_flow import FakeClient
from backend.tests.passport.test_builder import _database, _seed_change
from backend.tests.passport.test_execution_boundary import (
    PACKAGE_SID, _appcontainer_launch, _facts, _seed_appcontainer_launch,
)
from backend.tests.passport.test_v2 import _launch
from backend.tests.providers.fakes import FakeHttpTransport, json_response

SEED = 20261003


def test_mock_change_with_hundreds_of_mixed_launches_binds_the_weakest(tmp_path) -> None:
    rng = random.Random(SEED)
    database = _database(tmp_path)
    change = _seed_change(database)
    expected: dict[str, str] = {}
    for _ in range(120):
        kind = rng.choice(["boxed", "boxed", "restricted", "unconfined", "corrupt"])
        if kind in ("boxed", "corrupt"):
            facts = _facts() if kind == "boxed" else _facts(job_verified=False)
            run_id = _seed_appcontainer_launch(database, change.id, facts=facts)
            expected[str(run_id)] = "APPCONTAINER" if kind == "boxed" else "UNKNOWN"
        else:
            run_id = _launch(database, change.id)
            with database.connection() as connection:
                raw = json.loads(connection.execute("SELECT payload_json FROM agent_runs WHERE id = ?",
                                                    (run_id,)).fetchone()["payload_json"])
                raw.update(restricted_token_applied=kind == "restricted", top_level_pid=7)
                connection.execute("UPDATE agent_runs SET payload_json = ? WHERE id = ?",
                                   (json.dumps(raw), run_id))
            expected[run_id] = "RESTRICTED_TOKEN" if kind == "restricted" else "UNCONFINED"
    snapshot = PassportV2Issuer(database).snapshot(change.id)
    observed = {str(item.run_id): item.boundary for item in snapshot.launch_boundaries}
    assert observed == expected
    order = ["UNCONFINED", "UNKNOWN", "RESTRICTED_TOKEN", "APPCONTAINER"]
    assert snapshot.execution_boundary == next(b for b in order if b in expected.values())


def test_mock_launch_records_never_claim_appcontainer_without_every_fact() -> None:
    rng = random.Random(SEED + 1)
    for _ in range(3000):
        facts = _facts(**{key: rng.choice([True, False, None, "0x2000", "x"])
                          for key in rng.sample(["is_appcontainer", "job_verified", "integrity_rid"], rng.randint(0, 2))})
        sid = rng.choice([PACKAGE_SID, "S-1-15-2-9"])
        run_id = uuid4()
        launch = _appcontainer_launch(_facts(), status=rng.choice(["PASSED", "FAILED", "ATTACHED"]))
        observed = launch_boundary(run_id, launch, [(sid, {"run_id": str(run_id), "facts": facts})])
        verified = (facts.get("is_appcontainer") is True and facts.get("job_verified") is True
                    and facts.get("integrity_rid") == "0x1000" and sid == PACKAGE_SID
                    and launch["status"] != "ATTACHED")
        assert (observed.boundary == "APPCONTAINER") == verified
        assert change_boundary([observed])[0] in {"APPCONTAINER", "UNKNOWN"}


def test_mock_changes_through_the_preset_gate(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from backend.app.passport import v2 as v2_module

    rng = random.Random(SEED + 2)
    database = _database(tmp_path)
    for _ in range(60):
        change = _seed_change(database)
        preset = rng.choice([None, "strict", "standard"])
        decision, freshness = rng.choice(["ALLOW", "DENY"]), rng.choice(["CURRENT", "STALE", "UNKNOWN"])
        if preset:
            change = change.model_copy(update={"contract": ChangeContract(
                schema_version=3, policy_preset_name=preset, policy_change_type="code")})
        monkeypatch.setattr(v2_module.PassportV2Issuer, "snapshot", lambda self, cid, d=decision, f=freshness:
                            SimpleNamespace(policy_decision=d, diff_coverage=SimpleNamespace(freshness=f)))
        expected = preset is None or (decision == "ALLOW" and freshness == "CURRENT")
        assert preset_allows(database, change) is expected


def test_mock_gitlab_projects_pass_only_when_every_required_job_succeeded() -> None:
    rng = random.Random(SEED + 3)
    sha = "a" * 40
    for _ in range(300):
        jobs = {f"job{i}": rng.choice(["success", "success", "failed", "running", "canceled", "skipped"])
                for i in range(rng.randint(0, 5))}
        required = rng.sample(sorted(jobs) + ["missing-job"], rng.randint(0, min(3, len(jobs) + 1)))
        statuses = [{"name": name, "status": status, "sha": sha} for name, status in jobs.items()]
        provider = GitLabProvider(FakeHttpTransport([json_response(200, statuses)]))
        verification = OutcomeTracker(provider).verify_required_checks(
            token="t", repository="g/p", expected_head_sha=sha, required_check_names=required)
        assert verification.all_passed == all(jobs.get(name) == "success" for name in required)
        assert verification.evidence_complete == all(name in jobs for name in required)


def test_mock_v8_coverage_never_executes_more_than_is_executable() -> None:
    rng = random.Random(SEED + 4)
    for _ in range(400):
        lines = [rng.choice(["a();", "", "  // c", "if (x) { y(); }", "/* b */ z();", "q();"])
                 for _ in range(rng.randint(1, 12))]
        source = "\n".join(lines) + "\n"
        ranges = [{"startOffset": rng.randint(0, len(source)), "endOffset": rng.randint(0, len(source)),
                   "count": rng.choice([0, 1, 3])} for _ in range(rng.randint(0, 6))]
        executable, executed = line_coverage(source, [{"ranges": ranges}])
        assert executed <= executable
        assert all(1 <= line <= len(lines) for line in executable)


def test_mock_run_flows_never_apply_without_a_clean_confirmed_preview() -> None:
    rng = random.Random(SEED + 5)
    for _ in range(400):
        preview = rng.choice([None, {"approval_token": "t", "refusal_reason": None, "changed_paths": []},
                              {"approval_token": None, "refusal_reason": "FORBIDDEN_PATH_IN_DIFF", "changed_paths": []}])
        client = FakeClient(agent_status=rng.choice(["PASSED", "PASSED", "FAILED"]), preview=preview,
                            workspace=rng.random() < 0.8, applied=rng.random() < 0.8,
                            fail_on=rng.choice([None, None, None, "launch", "preview", "current"]))
        confirm, apply = rng.random() < 0.5, rng.random() < 0.5
        report = RunFlow(client, confirm=lambda _p, c=confirm: c).run(uuid4(), uuid4(), RunOptions(
            executable="claude", args=[], apply=apply))
        if "apply" in client.calls:
            clean = (preview or client.preview).get("approval_token") and not (preview or client.preview).get("refusal_reason")
            assert apply and confirm and clean and client.agent_status == "PASSED" and client.workspace
        assert report.outcome in {"ok", "stopped", "failed"}
