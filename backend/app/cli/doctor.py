"""``sentinel doctor``: what this machine can and cannot do, and how to fix it.

Every check reuses the code path Sentinel itself runs (the same bwrap probe,
cgroup feasibility check and credential-store selection), so the doctor never
disagrees with a real launch. Read-only: nothing is created, moved or installed.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str
    remedy: str | None = None


def _python() -> Check:
    version = sys.version_info
    good = version >= (3, 12)
    return Check("python", OK if good else FAIL, f"{sys.version.split()[0]} at {sys.executable}",
                 None if good else "Sentinel needs Python 3.12 or newer.")


def _git() -> Check:
    git = shutil.which("git")
    return Check("git", OK if git else FAIL, git or "not found",
                 None if git else "Install Git; every Change is built from a Git repository.")


def _store() -> Check:
    from backend.app.core.evidence_store import default_store_directory

    directory = default_store_directory()
    return Check("store", OK, f"default evidence store: {directory}")


def _credentials() -> Check:
    from backend.app.credentials.selection import (
        CredentialStoreSelectionError,
        choose_credential_store_kind,
    )

    try:
        kind = choose_credential_store_kind()
    except CredentialStoreSelectionError as exc:
        return Check("credentials", FAIL, str(exc), "Fix SENTINEL_CREDENTIAL_STORE.")
    if kind == "file":
        return Check("credentials", WARN, "owner-only file store (readable by same-user processes)",
                     "Set SENTINEL_CREDENTIAL_STORE=keyring and install the 'keyring' extra to "
                     "use the Secret Service.")
    return Check("credentials", OK, f"{kind} store")


def _linux_sandbox() -> list[Check]:
    if not sys.platform.startswith("linux"):
        boundary = "AppContainer" if sys.platform == "win32" else "none (claude refuses to launch)"
        return [Check("linux_sandbox", SKIP, f"not Linux; agent boundary here: {boundary}")]
    from backend.app.core.errors import AppError
    from backend.app.execution.cgroups import CgroupHierarchy
    from backend.app.execution.linux_sandbox import (
        _apparmor_restricts_userns,
        find_bwrap,
        probe_bwrap,
    )

    checks: list[Check] = []
    try:
        bwrap = find_bwrap()
    except AppError as exc:
        return [Check("bwrap", FAIL, exc.message, "Install the 'bubblewrap' package.")]
    works, detail, disable_userns = probe_bwrap(bwrap)
    if works:
        checks.append(Check("bwrap", OK, f"{bwrap}: namespaces work"
                            + ("" if disable_userns else " (no --disable-userns: bwrap < 0.8)")))
    elif _apparmor_restricts_userns():
        checks.append(Check("bwrap", FAIL, f"{detail}; AppArmor restricts user namespaces",
                            "Install packaging/apparmor/sentinel-bwrap (read its trade-off "
                            "note first)."))
    else:
        checks.append(Check("bwrap", FAIL, detail,
                            "Enable unprivileged user namespaces (kernel.unprivileged_userns_clone, "
                            "user.max_user_namespaces)."))
    try:
        hierarchy = CgroupHierarchy.from_environment()
        hierarchy.check()
        checks.append(Check("cgroup", OK, f"delegated cgroup v2 subtree: {hierarchy.parent}"))
    except AppError as exc:
        checks.append(Check("cgroup", FAIL, exc.message,
                            "Run Sentinel in a systemd scope with Delegate=yes (systemd-run --user "
                            "--scope -p Delegate=yes ...) or set SENTINEL_CGROUP_PARENT to a "
                            "subtree this user owns."))
    return checks


def run_checks() -> list[Check]:
    return [_python(), _git(), _store(), _credentials(), *_linux_sandbox()]


def exit_code(checks: list[Check]) -> int:
    return 1 if any(check.status == FAIL for check in checks) else 0


def environment_summary() -> dict[str, str]:
    return {"platform": sys.platform, "cwd": os.getcwd()}
