"""Observed execution boundary of a Change's agent launches (Passport v2, additive).

A launch is APPCONTAINER only when the workspace run that leased the clone for
it recorded verified token facts (AppContainer token, Low integrity, Job Object
verified before resume) AND the launch record's authority text is exactly the
one those facts produce. Anything absent or inconsistent is UNKNOWN, never
APPCONTAINER. The Change-level claim is the weakest launch boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

from backend.app.execution.check_repository import verified_token_facts
from backend.app.execution.launcher import appcontainer_authority

Boundary = Literal["APPCONTAINER", "RESTRICTED_TOKEN", "UNCONFINED", "UNKNOWN"]

APPCONTAINER = "APPCONTAINER"
RESTRICTED_TOKEN = "RESTRICTED_TOKEN"
UNCONFINED = "UNCONFINED"
UNKNOWN = "UNKNOWN"
LOW_INTEGRITY_RID = "0x1000"

# A run that never spawned a process ran no agent code; it cannot weaken the claim.
_NOT_STARTED_STATUSES = frozenset({"ERROR", "CANCELLED"})


@dataclass(frozen=True, slots=True)
class LaunchBoundary:
    run_id: UUID
    boundary: Boundary
    package_sid: str | None
    reason: str | None  # why the boundary is not APPCONTAINER


def workspace_run_entries(records: Iterable[Any]) -> dict[str, list[tuple[str | None, Mapping[str, Any]]]]:
    """Every recorded workspace run of a Change by run ID, with its workspace's package SID."""

    entries: dict[str, list[tuple[str | None, Mapping[str, Any]]]] = {}
    for record in records:
        for run in record.runs:
            if isinstance(run, Mapping) and run.get("run_id"):
                entries.setdefault(str(run["run_id"]), []).append((record.package_sid, run))
    return entries


def _verified_facts(facts: Any, package_sid: str | None) -> bool:
    return (verified_token_facts(facts)
            and str(facts.get("integrity_rid", "")).lower() == LOW_INTEGRITY_RID
            and isinstance(facts.get("package_sid"), str)
            and package_sid is not None and facts["package_sid"] == package_sid
            and isinstance(facts.get("capability_sids", []), list)
            and all(isinstance(sid, str) for sid in facts.get("capability_sids", [])))


def _rendered_authority(facts: Mapping[str, Any]) -> str | None:
    """The authority text these facts produce; None when they cannot be rendered."""
    try:
        return appcontainer_authority(SimpleNamespace(**facts))
    except (TypeError, ValueError):
        return None


def launch_boundary(run_id: UUID, launch: Mapping[str, Any],
                    entries: Sequence[tuple[str | None, Mapping[str, Any]]]) -> LaunchBoundary:
    """Classify one launch record against the workspace runs recorded for it."""

    status = launch.get("status")
    authority = launch.get("authority_reduction")
    if status == "ATTACHED":
        return LaunchBoundary(run_id, UNKNOWN, None,
                              "an attached run was not launched by Sentinel")
    if entries:
        if len(entries) != 1:
            return LaunchBoundary(run_id, UNKNOWN, None,
                                  "more than one workspace run is recorded for a launch")
        package_sid, entry = entries[0]
        facts = entry.get("facts")
        if facts is None:
            return LaunchBoundary(run_id, UNKNOWN, None,
                                  "the workspace run recorded no verified launch")
        if not _verified_facts(facts, package_sid):
            return LaunchBoundary(run_id, UNKNOWN, None,
                                  "the workspace run's boundary facts did not verify")
        expected = _rendered_authority(facts)
        if not isinstance(authority, str) or expected is None or authority != expected:
            return LaunchBoundary(run_id, UNKNOWN, None,
                                  "the launch record does not match its boundary facts")
        return LaunchBoundary(run_id, APPCONTAINER, facts["package_sid"], None)
    if isinstance(authority, str) and authority.startswith("AppContainer boundary verified"):
        return LaunchBoundary(run_id, UNKNOWN, None,
                              "an AppContainer launch has no workspace run record")
    if not isinstance(launch.get("restricted_token_applied"), bool):
        return LaunchBoundary(run_id, UNKNOWN, None,
                              "the launch record carries no token facts")
    if launch["restricted_token_applied"]:
        return LaunchBoundary(run_id, RESTRICTED_TOKEN, None,
                              "a launch ran with a reduced token, not an AppContainer")
    if status in _NOT_STARTED_STATUSES and launch.get("top_level_pid") is None:
        return LaunchBoundary(run_id, UNKNOWN, None, None)
    return LaunchBoundary(run_id, UNCONFINED, None,
                          "a launch ran with the existing account authority")


def change_boundary(launches: Sequence[LaunchBoundary]) -> tuple[Boundary, str | None]:
    """The weakest launch boundary; UNKNOWN when no launch ran agent code."""

    ran = [item for item in launches if item.boundary != UNKNOWN or item.reason is not None]
    if not ran:
        return UNKNOWN, "no launch of this Change ran agent code under an observed boundary"
    for weakest in (UNCONFINED, UNKNOWN, RESTRICTED_TOKEN):
        for item in ran:
            if item.boundary == weakest:
                return weakest, item.reason
    return APPCONTAINER, None
