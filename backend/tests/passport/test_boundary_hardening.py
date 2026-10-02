"""Adversarial coverage for the bound execution boundary and the preset lifecycle gate."""

from __future__ import annotations

import base64
import io
import json
import os
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.contracts.models import ChangeContract, PassportV2Payload
from backend.app.core.config import Settings
from backend.app.core.errors import AppError
from backend.app.credentials.memory_store import InMemoryCredentialStore
from backend.app.main import create_app
from backend.app.passport import v2 as v2_module
from backend.app.passport.boundary import change_boundary, launch_boundary
from backend.app.passport.bundle import BundleExporter
from backend.app.passport.cng import CngKey
from backend.app.passport.jcs import canonicalize, parse_canonical
from backend.app.passport.trust import TrustRegistry
from backend.app.passport.v2 import PassportV2Issuer
from backend.app.passport.verify import verify_bundle
from backend.app.policy.gate import preset_allows
from backend.tests.passport.test_builder import _database, _seed_change
from backend.tests.passport.test_execution_boundary import (
    PACKAGE_SID, _appcontainer_launch, _facts, _seed_appcontainer_launch,
)
from backend.tests.passport.test_v2 import _launch
from backend.tests.passport.test_verify import _rewrite


# --------------------------------------------------------------------- classification


@pytest.mark.parametrize("facts", [
    "not-a-mapping", ["is_appcontainer", True], 42,
    {"is_appcontainer": True, "job_verified": True},  # no integrity, no package SID
    {**_facts(), "package_sid": None},
    {**_facts(), "is_appcontainer": "true"},  # truthy string is not a verified fact
    {**_facts(), "job_verified": 1},
])
def test_malformed_facts_never_crash_or_claim_appcontainer(facts) -> None:
    run_id = uuid4()
    observed = launch_boundary(run_id, _appcontainer_launch(_facts()),
                               [(PACKAGE_SID, {"run_id": str(run_id), "facts": facts})])
    assert observed.boundary == "UNKNOWN"


def test_two_workspace_runs_for_one_launch_are_unknown() -> None:
    facts = _facts()
    run_id = uuid4()
    entry = (PACKAGE_SID, {"run_id": str(run_id), "facts": facts})
    observed = launch_boundary(run_id, _appcontainer_launch(facts), [entry, entry])
    assert observed.boundary == "UNKNOWN" and "more than one" in observed.reason


def test_attached_launch_is_unknown_even_with_verified_workspace_facts() -> None:
    facts = _facts()
    run_id = uuid4()
    observed = launch_boundary(run_id, _appcontainer_launch(facts, status="ATTACHED"),
                               [(PACKAGE_SID, {"run_id": str(run_id), "facts": facts})])
    assert observed.boundary == "UNKNOWN"


@pytest.mark.parametrize("token_fact", ["true", 1, None, "yes"])
def test_non_boolean_token_facts_are_unknown(token_fact) -> None:
    launch = {"status": "PASSED", "top_level_pid": 7, "restricted_token_applied": token_fact}
    assert launch_boundary(uuid4(), launch, []).boundary == "UNKNOWN"


@pytest.mark.parametrize("authority", [None, 7, "", "AppContainer boundary verified (edited)"])
def test_authority_text_must_be_exactly_the_facts_rendering(authority) -> None:
    facts = _facts()
    run_id = uuid4()
    observed = launch_boundary(run_id, _appcontainer_launch(facts, authority_reduction=authority),
                               [(PACKAGE_SID, {"run_id": str(run_id), "facts": facts})])
    assert observed.boundary == "UNKNOWN"


def test_capability_sids_are_bound_through_the_authority_text() -> None:
    # The launch record names no capabilities; facts claiming one must not verify.
    granted = _facts(capability_sids=["S-1-15-3-1"])
    run_id = uuid4()
    launch = _appcontainer_launch(_facts())  # authority rendered without the capability
    observed = launch_boundary(run_id, launch,
                               [(PACKAGE_SID, {"run_id": str(run_id), "facts": granted})])
    assert observed.boundary == "UNKNOWN"


def test_weakest_ordering_is_independent_of_launch_order() -> None:
    def make(**launch):
        return launch_boundary(uuid4(), {"status": "PASSED", "top_level_pid": 1, **launch}, [])
    restricted = make(restricted_token_applied=True)
    unconfined = make(restricted_token_applied=False)
    unknown = make()  # no token facts
    for order in ([restricted, unconfined, unknown], [unknown, restricted, unconfined],
                  [unconfined, unknown, restricted]):
        assert change_boundary(order)[0] == "UNCONFINED"
    assert change_boundary([restricted, unknown])[0] == "UNKNOWN"
    assert change_boundary([unknown, restricted])[0] == "UNKNOWN"


# ------------------------------------------------------------------------ snapshot


def test_mixed_launches_bind_each_boundary_in_launch_order(tmp_path) -> None:
    database = _database(tmp_path)
    change = _seed_change(database)
    boxed = _seed_appcontainer_launch(database, change.id, facts=_facts())
    generic = _launch(database, change.id)
    with database.connection() as connection:
        raw = json.loads(connection.execute("SELECT payload_json FROM agent_runs WHERE id = ?",
                                            (generic,)).fetchone()["payload_json"])
        raw.update(restricted_token_applied=True, top_level_pid=99)
        connection.execute("UPDATE agent_runs SET payload_json = ? WHERE id = ?",
                           (json.dumps(raw), generic))
    snapshot = PassportV2Issuer(database).snapshot(change.id)
    assert snapshot.execution_boundary == "RESTRICTED_TOKEN"
    assert ([str(item.run_id) for item in snapshot.launch_boundaries]
            == [str(item.run_id) for item in snapshot.launch_records])
    by_run = {str(item.run_id): item.boundary for item in snapshot.launch_boundaries}
    assert by_run == {str(boxed): "APPCONTAINER", generic: "RESTRICTED_TOKEN"}
    assert any(line.startswith("Execution boundary RESTRICTED_TOKEN:")
               for line in snapshot.limitations)


def test_tampered_workspace_facts_after_launch_drop_the_claim(tmp_path) -> None:
    database = _database(tmp_path)
    change = _seed_change(database)
    _seed_appcontainer_launch(database, change.id, facts=_facts())
    assert PassportV2Issuer(database).snapshot(change.id).execution_boundary == "APPCONTAINER"
    with database.connection() as connection:
        row = connection.execute("SELECT id, payload_json FROM change_workspaces").fetchone()
        payload = json.loads(row["payload_json"])
        payload["runs"][0]["facts"]["job_verified"] = False
        connection.execute("UPDATE change_workspaces SET payload_json = ? WHERE id = ?",
                           (json.dumps(payload), row["id"]))
    assert PassportV2Issuer(database).snapshot(change.id).execution_boundary == "UNKNOWN"


def test_no_launches_is_unknown_with_a_limitation(tmp_path) -> None:
    database = _database(tmp_path)
    snapshot = PassportV2Issuer(database).snapshot(_seed_change(database).id)
    assert snapshot.execution_boundary == "UNKNOWN" and snapshot.launch_boundaries == []
    assert any(line.startswith("Execution boundary UNKNOWN:") for line in snapshot.limitations)


def test_pre_boundary_claims_still_validate_without_launch_boundaries() -> None:
    claims = PassportV2Payload.model_validate({
        "change_id": str(uuid4()), "change_revision": 1, "lifecycle_state": "ACTIVE",
        "risk_level": "LOW", "journal_event_count": 0, "journal_integrity": "UNKNOWN",
        "launch_records": [], "issued_at": datetime.now(UTC).isoformat()})
    assert claims.execution_boundary == "UNKNOWN" and claims.launch_boundaries == []


# ----------------------------------------------------------------- signed bundle


@pytest.fixture(scope="module")
def boxed_bundle(tmp_path_factory: pytest.TempPathFactory):
    if os.name != "nt":
        pytest.skip("Windows CNG required")
    root = tmp_path_factory.mktemp("boundary-bundle")
    database = _database(root)
    change = _seed_change(database)
    _seed_appcontainer_launch(database, change.id, facts=_facts())
    key_name = f"Sentinel disposable test {uuid4()}"
    try:
        bundle = BundleExporter(database, key_name=key_name,
                                installation_label="Lab").export(change.id)
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            signature = parse_canonical(archive.read("signature.json"))
        path = root / bundle.filename
        path.write_bytes(bundle.content)
        trust = TrustRegistry(root / "trust.json")
        trust.add(spki=base64.b64decode(signature["public_spki_b64"]), label="Lab")
        yield path, trust, root
    finally:
        with CngKey.open(name=key_name) as key:
            key.delete_for_test()


def test_bundle_carries_and_verifies_the_appcontainer_boundary(boxed_bundle) -> None:
    path, trust, _root = boxed_bundle
    result = verify_bundle(path, trust=trust)
    assert result.verdict == "VALID", result.reason
    assert result.claims["execution_boundary"] == "APPCONTAINER"
    with zipfile.ZipFile(path) as archive:
        claims = parse_canonical(archive.read("passport.json"))["claims"]
    assert [item["boundary"] for item in claims["launch_boundaries"]] == ["APPCONTAINER"]


@pytest.mark.parametrize("forge", ["downgrade_claim", "drop_list", "edit_sid"])
def test_editing_bound_boundary_claims_invalidates_the_bundle(boxed_bundle, forge) -> None:
    path, trust, root = boxed_bundle
    with zipfile.ZipFile(path) as archive:
        passport = parse_canonical(archive.read("passport.json"))
    claims = passport["claims"]
    if forge == "downgrade_claim":
        claims["execution_boundary"] = "UNCONFINED"
    elif forge == "drop_list":
        del claims["launch_boundaries"]
    else:
        claims["launch_boundaries"][0]["package_sid"] = "S-1-15-2-9-9-9"
    forged = _rewrite(path, root / f"forged-{forge}.sentinel",
                      replacement={"passport.json": canonicalize(passport)})
    assert verify_bundle(forged, trust=trust).verdict == "INVALID"


# --------------------------------------------------------------------- preset gate


def _preset_change(database):
    change = _seed_change(database)
    contract = ChangeContract(schema_version=3, policy_preset_name="standard",
                              policy_change_type="code")
    with database.connection() as connection:
        connection.execute("UPDATE changes SET contract_json = ? WHERE id = ?",
                           (contract.model_dump_json(), str(change.id)))
    return change.model_copy(update={"contract": contract})


@pytest.mark.parametrize("decision, freshness, expected", [
    ("ALLOW", "CURRENT", True),
    ("ALLOW", "STALE", False),
    ("ALLOW", "UNKNOWN", False),
    ("DENY", "CURRENT", False),
])
def test_gate_needs_a_current_allow(tmp_path, monkeypatch, decision, freshness, expected) -> None:
    database = _database(tmp_path)
    change = _preset_change(database)
    fake = SimpleNamespace(policy_decision=decision,
                           diff_coverage=SimpleNamespace(freshness=freshness))
    monkeypatch.setattr(v2_module.PassportV2Issuer, "snapshot", lambda self, change_id: fake)
    assert preset_allows(database, change) is expected


@pytest.mark.parametrize("error", [
    AppError("PASSPORT_LAUNCH_IN_PROGRESS", "in progress", status_code=409),
    OSError("disk"), ValueError("bad row"),
])
def test_gate_closes_when_the_snapshot_cannot_be_built(tmp_path, monkeypatch, error) -> None:
    database = _database(tmp_path)
    change = _preset_change(database)

    def fail(self, change_id):
        raise error
    monkeypatch.setattr(v2_module.PassportV2Issuer, "snapshot", fail)
    assert preset_allows(database, change) is False


def test_gate_without_preset_never_builds_a_snapshot(tmp_path, monkeypatch) -> None:
    database = _database(tmp_path)

    def explode(self, change_id):
        raise AssertionError("snapshot must not be built without a preset")
    monkeypatch.setattr(v2_module.PassportV2Issuer, "snapshot", explode)
    assert preset_allows(database, _seed_change(database)) is True


def test_application_wires_the_preset_gate_into_lifecycle_facts(tmp_path) -> None:
    app = create_app(settings=Settings(database_path=tmp_path / "state" / "api.sqlite3"),
                     credential_store=InMemoryCredentialStore())
    with TestClient(app):  # startup initializes the database
        facts_port = app.state.change_service.lifecycle_facts
        database = app.state.change_service.repository.database
        plain = _seed_change(database)
        gated = _preset_change(database)
        assert facts_port.get_facts(plain, "PR_OPEN").preset_allowed is True
        assert facts_port.get_facts(gated, "PR_OPEN").preset_allowed is False
        assert facts_port.get_facts(gated, "REVIEW_READY").preset_allowed is False
