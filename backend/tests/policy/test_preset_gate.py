"""A selected policy preset gates REVIEW_READY and PR_OPEN on a current ALLOW."""

from __future__ import annotations

import pytest

from backend.app.contracts.models import ChangeContract, ChangeLifecycleState, LifecycleFacts
from backend.app.core.errors import AppError
from backend.app.core.lifecycle import validate_transition
from backend.app.core.lifecycle_facts_service import RuntimeLifecycleFacts
from backend.app.core.runtime_repositories import (
    OutcomeRepository, ProviderOperationRepository, RecoveryRepository,
)
from backend.app.identity.repository import DelegationRepository
from backend.app.policy.gate import preset_allows
from backend.tests.passport.test_builder import _database, _seed_change

_S = ChangeLifecycleState


def _with_preset(database, change):
    contract = ChangeContract(schema_version=3, policy_preset_name="standard",
                              policy_change_type="code")
    with database.connection() as connection:
        connection.execute("UPDATE changes SET contract_json = ? WHERE id = ?",
                           (contract.model_dump_json(), str(change.id)))
    return change.model_copy(update={"contract": contract})


def test_gate_is_open_without_a_selected_preset(tmp_path) -> None:
    database = _database(tmp_path)
    assert preset_allows(database, _seed_change(database)) is True


def test_selected_preset_without_evidence_keeps_gate_closed(tmp_path) -> None:
    database = _database(tmp_path)
    change = _with_preset(database, _seed_change(database))
    # No diff coverage was measured, so the preset decides DENY.
    assert preset_allows(database, change) is False


@pytest.mark.parametrize("current, target, facts", [
    (_S.LOCALLY_VERIFIED, _S.REVIEW_READY,
     LifecycleFacts(deviations_resolved=True, required_evidence_complete=True)),
    (_S.REVIEW_READY, _S.PR_OPEN, LifecycleFacts(pull_request_recorded=True)),
])
def test_transition_names_the_missing_preset_allow(current, target, facts) -> None:
    with pytest.raises(AppError) as failure:
        validate_transition(current, target, facts)
    assert failure.value.code == "TRANSITION_GUARD_FAILED"
    assert failure.value.details["missing_requirements"] == ["preset_allowed"]


def test_facts_service_consults_the_gate_only_for_gated_targets(tmp_path) -> None:
    database = _database(tmp_path)
    change = _seed_change(database)
    calls: list[str] = []

    def deny(view) -> bool:
        calls.append(str(view.id))
        return False

    facts = RuntimeLifecycleFacts(
        DelegationRepository(database), ProviderOperationRepository(database),
        OutcomeRepository(database), RecoveryRepository(database), preset_allows=deny)
    assert facts.get_facts(change, "PR_OPEN").preset_allowed is False
    assert facts.get_facts(change, "REVIEW_READY").preset_allowed is False
    assert len(calls) == 2
    facts.get_facts(change, "ACTIVE")
    assert len(calls) == 2
    unwired = RuntimeLifecycleFacts(
        DelegationRepository(database), ProviderOperationRepository(database),
        OutcomeRepository(database), RecoveryRepository(database))
    assert unwired.get_facts(change, "PR_OPEN").preset_allowed is True
