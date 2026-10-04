"""Honest capability reporting for configured, pending, and removed systems."""

from __future__ import annotations

import sys

from backend.app.contracts.models import (
    CapabilitiesResponse,
    Capability,
    CapabilityState,
)


_REMOVED = {
    "filesystem_tracker": "Removed by approved scope; file writes and before-images are not observed.",
}

_RETAINED = {
    "change_lifecycle": "Change contracts, revisions, guarded lifecycle transitions, and migrations.",
    "git_inspection": "Read-only Git working-tree inspection.",
    "legacy_verification": "Bounded compatibility verification command execution.",
    "git_checkpoints": "Named Git checkpoint capture and comparison.",
    "agent_launcher": "Launch/attach summaries; launched Windows runs use Job Object process-tree supervision.",
    "process_supervisor": "Windows Job Object descendant attribution and cleanup with a restricted token whose maximum privileges are disabled; caller integrity is retained for repository writes; not a sandbox.",
    "environment_passports": "Redacted environment capture and drift comparison.",
    "dependency_tracking": "Supported manifest and lockfile comparison.",
    "assurance": "Evidence-selected checks and coverage reporting.",
    "identity_and_policy": "Actors, delegations, and scoped policy decisions.",
    "credential_broker": "OS-backed provider credential brokering.",
    "provider_outcomes": "GitHub pull-request and CI outcome tracking.",
    "recovery": "Approved Git/provider compensation plus termination of process trees still tracked by this daemon instance.",
    "change_passport": "Versioned retained-evidence export.",
    "cli_and_terminal_ui": "Scriptable and interactive terminal experience.",
    "event_journal": "Per-Change hash-chained mutation record covering entities this backend already models.",
    "replay": "Trace-only reconstruction and cryptographic verification of a Change's causal timeline; no re-execution.",
    "tool_registry": "Top-level launched executable and declared-manifest trust lifecycle with Windows-Authenticode signature checks.",
}


# Capabilities whose only implementation today is Windows-specific (Job Objects).
# Off Windows they are UNSUPPORTED, whatever was configured, until a verified
# Linux boundary exists (HANDOFF Phase 1).
_WINDOWS_ONLY = {
    "process_supervisor": "Process-tree supervision uses Windows Job Objects; no Linux or "
                          "macOS equivalent is implemented yet.",
}
# Capabilities that work off Windows, but with less than they claim on Windows.
_OFF_WINDOWS_LIMITATIONS = {
    "agent_launcher": "Off Windows, boundary adapters (claude, AppContainer) fail closed because "
                      "no verified Linux or macOS boundary exists yet; generic launches run "
                      "without descendant supervision or a restricted token, and say so.",
}


def unsupported_on_platform(platform: str | None = None) -> dict[str, str]:
    """Capability id -> reason for capabilities this platform cannot provide."""
    resolved = sys.platform if platform is None else platform
    return {} if resolved == "win32" else dict(_WINDOWS_ONLY)


def build_capabilities(configured: set[str], *, platform: str | None = None) -> CapabilitiesResponse:
    items: list[Capability] = []
    unsupported = unsupported_on_platform(platform)
    on_windows = (sys.platform if platform is None else platform) == "win32"
    for capability_id, description in _RETAINED.items():
        available = capability_id in configured
        if capability_id in unsupported:
            state, reason = CapabilityState.UNSUPPORTED, unsupported[capability_id]
        elif available:
            state, reason = CapabilityState.AVAILABLE, None
        else:
            state, reason = (CapabilityState.UNCONFIGURED,
                             "A retained implementation is not connected yet.")
        limitations = [description]
        if not on_windows and capability_id in _OFF_WINDOWS_LIMITATIONS:
            limitations.append(_OFF_WINDOWS_LIMITATIONS[capability_id])
        items.append(
            Capability(
                id=capability_id,
                name=capability_id.replace("_", " ").title(),
                state=state,
                reason=reason,
                limitations=limitations,
            )
        )
    for capability_id, reason in _REMOVED.items():
        items.append(
            Capability(
                id=capability_id,
                name=capability_id.replace("_", " ").title(),
                state=CapabilityState.UNSUPPORTED,
                reason=reason,
            )
        )
    return CapabilitiesResponse(items=items)
