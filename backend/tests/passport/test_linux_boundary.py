"""LINUX_SANDBOX in the Passport: claimed only from re-verified recorded facts (every OS)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.app.execution.launcher import linux_sandbox_authority
from backend.app.passport.boundary import LaunchBoundary, change_boundary, launch_boundary

IDENTITY = "linux-sandbox:sentinel.w.0123456789abcdef0123456789abcdef"


def _facts(**overrides) -> dict:
    host = {name: f"{name}:[4026531{index:03d}]" for index, name in
            enumerate(("user", "mnt", "pid", "ipc", "uts", "cgroup", "net"))}
    sandbox = {name: f"{name}:[4026532{index:03d}]" for index, name in enumerate(host)}
    facts = {
        "pid": 4242, "namespaces": sandbox, "host_namespaces": host, "seccomp_mode": "2",
        "seccomp_filters": 2, "supervisor_seccomp_filters": 1, "no_new_privs": "1",
        "uid_map": ["0 1000 1"], "cgroup": "/sentinel/sentinel-run-ab", "expected_cgroup":
        "/sentinel/sentinel-run-ab", "network_isolated": True, "verified": True, "failures": [],
        "sandbox_kind": "linux_sandbox",
    }
    facts.update(overrides)
    return facts


def _launch(facts: dict, **overrides) -> dict:
    launch = {"status": "PASSED", "authority_reduction": linux_sandbox_authority(facts),
              "restricted_token_applied": False, "top_level_pid": 4242}
    launch.update(overrides)
    return launch


def test_verified_facts_and_matching_record_claim_linux_sandbox() -> None:
    facts = _facts()
    result = launch_boundary(uuid4(), _launch(facts), [(IDENTITY, {"facts": facts})])
    assert result.boundary == "LINUX_SANDBOX" and result.reason is None
    assert result.package_sid == IDENTITY


def test_shared_network_is_still_verified_when_the_profile_declared_it() -> None:
    facts = _facts(network_isolated=False)
    facts["namespaces"]["net"] = facts["host_namespaces"]["net"]
    result = launch_boundary(uuid4(), _launch(facts), [(IDENTITY, {"facts": facts})])
    assert result.boundary == "LINUX_SANDBOX"


@pytest.mark.parametrize("override", [
    {"verified": False},
    {"failures": ["uid_map does not map exactly this user"]},
    {"seccomp_mode": "0"},
    {"seccomp_filters": 1},
    {"no_new_privs": "0"},
    {"cgroup": "/elsewhere"},
    {"cgroup": None},
    {"network_isolated": True, "namespaces": None},
])
def test_any_failed_or_tampered_fact_is_unknown(override) -> None:
    facts = _facts(**override)
    authority = linux_sandbox_authority(_facts())  # what an honest run would have recorded
    result = launch_boundary(uuid4(), _launch(facts, authority_reduction=authority),
                             [(IDENTITY, {"facts": facts})])
    assert result.boundary == "UNKNOWN" and result.reason


def test_a_shared_namespace_is_unknown() -> None:
    facts = _facts()
    facts["namespaces"]["mnt"] = facts["host_namespaces"]["mnt"]
    authority = linux_sandbox_authority(_facts())
    result = launch_boundary(uuid4(), _launch(facts, authority_reduction=authority),
                             [(IDENTITY, {"facts": facts})])
    assert result.boundary == "UNKNOWN"


def test_a_record_that_does_not_match_its_facts_is_unknown() -> None:
    facts = _facts()
    forged = linux_sandbox_authority(_facts(cgroup="/other", expected_cgroup="/other"))
    result = launch_boundary(uuid4(), _launch(facts, authority_reduction=forged),
                             [(IDENTITY, {"facts": facts})])
    assert result.boundary == "UNKNOWN"
    assert "does not match" in result.reason


def test_linux_facts_on_a_windows_workspace_are_never_claimed() -> None:
    facts = _facts()
    result = launch_boundary(uuid4(), _launch(facts),
                             [("S-1-15-2-1-2-3-4-5-6-7", {"facts": facts})])
    assert result.boundary == "UNKNOWN"
    assert "non-Linux workspace" in result.reason


def test_a_linux_authority_without_a_workspace_run_is_unknown() -> None:
    result = launch_boundary(uuid4(), _launch(_facts()), [])
    assert result.boundary == "UNKNOWN"
    assert "no workspace run record" in result.reason


def test_linux_facts_are_never_relabeled_appcontainer() -> None:
    facts = _facts()
    claimed = launch_boundary(uuid4(), _launch(facts), [(IDENTITY, {"facts": facts})])
    assert claimed.boundary != "APPCONTAINER"


def _boundary(name: str, reason: str | None = None) -> LaunchBoundary:
    return LaunchBoundary(uuid4(), name, None, reason)


def test_one_strength_class_keeps_each_launchs_own_name() -> None:
    assert change_boundary([_boundary("LINUX_SANDBOX"), _boundary("LINUX_SANDBOX")]) == (
        "LINUX_SANDBOX", None)
    assert change_boundary([_boundary("APPCONTAINER")]) == ("APPCONTAINER", None)
    claim, reason = change_boundary([_boundary("LINUX_SANDBOX"), _boundary("APPCONTAINER")])
    assert claim == "LINUX_SANDBOX"
    assert "one strength class" in reason and "APPCONTAINER" in reason


@pytest.mark.parametrize("weaker", ["UNCONFINED", "RESTRICTED_TOKEN"])
def test_a_weaker_launch_still_wins(weaker) -> None:
    claim, _ = change_boundary([_boundary("LINUX_SANDBOX"), _boundary(weaker, "weaker run")])
    assert claim == weaker


@pytest.mark.parametrize(("boundary", "allowed"), [
    ("LINUX_SANDBOX", True), ("APPCONTAINER", True), ("RESTRICTED_TOKEN", False),
    ("UNCONFINED", False), ("UNKNOWN", False)])
def test_strict_preset_accepts_either_verified_boundary(boundary, allowed) -> None:
    from backend.app.policy.presets import PresetEvidence, evaluate_preset

    evidence = PresetEvidence(checks_passed=True, diff_exercised="PASS", measured_percent=100,
                              freshness="CURRENT", confined_checks="PASS",
                              execution_boundary=boundary)
    decision = evaluate_preset(preset_name="strict", change_type="code", evidence=evidence)
    assert (decision.status == "ALLOW") is allowed, (boundary, decision.reasons)
