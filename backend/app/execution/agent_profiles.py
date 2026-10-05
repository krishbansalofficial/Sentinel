"""Per-adapter runtime profiles: which launch boundary each agent adapter gets.

The boundary is a declared property of each adapter's :class:`RuntimeProfile`
(user decision D-01), resolved once per launch by :func:`resolve_profile`. It is
never chosen by platform detection or by exception handling: an adapter whose
profile says ``APPCONTAINER`` either launches inside a verified AppContainer or
does not launch at all, and an adapter whose profile says ``UNAVAILABLE``
(``codex``, D-02) always fails closed.

D-03 (kb, 2026-10-03): a profile may declare its boundary per platform in
``boundaries``, e.g. ``claude`` is ``{"win32": APPCONTAINER, "linux":
LINUX_SANDBOX}``. The declared entry for the running platform is the boundary;
a platform the map does not name resolves to ``UNAVAILABLE`` and fails closed.
That is still a declared property, not a fallback: no platform ever gets a
weaker boundary than the one written down for it.

Built-in profiles cannot be overridden by callers. Pure module: no Win32, no
process starts and no file I/O.
"""

from __future__ import annotations

import dataclasses
import ntpath
import os
import re
import sys
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class BoundaryKind(StrEnum):
    RESTRICTED_TOKEN = "restricted_token"
    APPCONTAINER = "appcontainer"
    LINUX_SANDBOX = "linux_sandbox"
    UNAVAILABLE = "unavailable"


# Boundaries that only exist on one platform; declaring one elsewhere is refused.
_PLATFORM_OF = {BoundaryKind.APPCONTAINER: "win32", BoundaryKind.LINUX_SANDBOX: "linux"}


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """How one adapter is launched.

    ``static_env`` holds fixed ``(key, value)`` pairs added to an AppContainer
    environment; it can never replace a key the boundary itself controls
    (see :data:`PROTECTED_ENV_KEYS`).
    """

    adapter: str
    boundary: BoundaryKind
    capabilities: tuple[str, ...] = ()
    tool_snapshot: bool = False
    staged_home: bool = False
    credential_kind: str | None = None
    static_env: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    requires_native_executable: bool = False
    git_bash_env: bool = False
    unavailable_reason: str | None = None
    # D-03: (platform, boundary) pairs; when set, the entry for the running
    # platform replaces ``boundary`` and an unnamed platform is UNAVAILABLE.
    boundaries: tuple[tuple[str, BoundaryKind], ...] = ()


# Keys an AppContainer environment derives from the boundary itself (container
# folder, staged home, known folders). A profile can never override them.
PROTECTED_ENV_KEYS = frozenset({
    "PATH", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME",
    "APPDATA", "HOMEDRIVE", "HOMEPATH", "COMSPEC",
})
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_BASE_KEYS = ("SystemRoot", "windir", "COMSPEC", "LOCALAPPDATA", "TEMP", "TMP")

NO_PLATFORM_BOUNDARY_REASON = (
    "The {adapter} adapter declares no verified boundary for this platform ({platform}), so "
    "Sentinel refuses to launch it rather than run it without one."
)
CODEX_UNAVAILABLE_REASON = (
    "The codex adapter has no tested AppContainer runtime profile yet, so Sentinel refuses "
    "to launch it rather than run it without a verified boundary."
)

BUILTIN_PROFILES: Mapping[str, RuntimeProfile] = types.MappingProxyType({
    "generic": RuntimeProfile("generic", BoundaryKind.RESTRICTED_TOKEN),
    "claude": RuntimeProfile(
        "claude", BoundaryKind.APPCONTAINER,
        capabilities=("internetClient",),
        tool_snapshot=True,
        staged_home=True,
        credential_kind="claude-oauth-file",
        # Validated in spike 004: no self-update inside the box, no system Git
        # config, no interactive Git credential prompt.
        static_env=(
            ("DISABLE_AUTOUPDATER", "1"),
            ("GIT_CONFIG_NOSYSTEM", "1"),
            ("GIT_TERMINAL_PROMPT", "0"),
        ),
        requires_native_executable=True,
        git_bash_env=True,
        boundaries=(("win32", BoundaryKind.APPCONTAINER), ("linux", BoundaryKind.LINUX_SANDBOX)),
    ),
    "codex": RuntimeProfile(
        "codex", BoundaryKind.UNAVAILABLE, unavailable_reason=CODEX_UNAVAILABLE_REASON,
    ),
})


def validate_extra_profiles(
    extra: Mapping[str, RuntimeProfile] | None,
) -> Mapping[str, RuntimeProfile]:
    """A read-only copy of caller-supplied profiles, or ``ValueError``.

    Built-in adapter names cannot be redefined (so ``claude`` can never be
    moved onto the restricted-token path), every profile must name its own
    adapter and declare exactly one :class:`BoundaryKind`, and ``static_env``
    may not override a protected key.
    """

    validated: dict[str, RuntimeProfile] = {}
    for name, profile in (extra or {}).items():
        if name in BUILTIN_PROFILES:
            raise ValueError(f"The built-in runtime profile '{name}' cannot be overridden.")
        if not isinstance(profile, RuntimeProfile):
            raise ValueError(f"The runtime profile for '{name}' is not a RuntimeProfile.")
        if profile.adapter != name:
            raise ValueError(f"The runtime profile for '{name}' names another adapter.")
        if not isinstance(profile.boundary, BoundaryKind):
            raise ValueError(f"The runtime profile for '{name}' has no valid boundary.")
        if profile.boundary is BoundaryKind.UNAVAILABLE and not profile.unavailable_reason:
            raise ValueError(f"The unavailable runtime profile '{name}' needs a reason.")
        seen: set[str] = set()
        for platform, kind in profile.boundaries:
            if not isinstance(platform, str) or not platform or platform in seen:
                raise ValueError(f"The runtime profile '{name}' repeats or omits a platform.")
            seen.add(platform)
            if not isinstance(kind, BoundaryKind):
                raise ValueError(f"The runtime profile '{name}' has no valid boundary for {platform}.")
            if _PLATFORM_OF.get(kind, platform) != platform:
                raise ValueError(
                    f"The runtime profile '{name}' declares {kind.value} for {platform}, where "
                    "that boundary does not exist.")
        for key, value in profile.static_env:
            if not _ENV_KEY.fullmatch(key) or not isinstance(value, str) or "\0" in value:
                raise ValueError(f"The runtime profile '{name}' has an invalid environment key.")
            if key.upper() in PROTECTED_ENV_KEYS:
                raise ValueError(
                    f"The runtime profile '{name}' may not override the protected key {key}.")
        validated[name] = profile
    return types.MappingProxyType(validated)


def for_platform(profile: RuntimeProfile, platform: str) -> RuntimeProfile:
    """``profile`` with ``boundary`` set to its declared entry for ``platform`` (D-03)."""

    if not profile.boundaries:
        return profile
    declared = dict(profile.boundaries)
    if platform in declared:
        return dataclasses.replace(profile, boundary=declared[platform])
    return dataclasses.replace(
        profile, boundary=BoundaryKind.UNAVAILABLE,
        unavailable_reason=NO_PLATFORM_BOUNDARY_REASON.format(adapter=profile.adapter,
                                                              platform=platform))


def resolve_profile(
    adapter: str, extra: Mapping[str, RuntimeProfile] | None = None, *,
    platform: str | None = None,
) -> RuntimeProfile:
    """The profile for ``adapter`` on ``platform`` (default: this one).

    Built-in first, then ``extra``, else restricted token; a per-platform map
    is resolved by :func:`for_platform`.
    """

    resolved_platform = sys.platform if platform is None else platform
    builtin = BUILTIN_PROFILES.get(adapter)
    if builtin is not None:
        return for_platform(builtin, resolved_platform)
    if extra is not None and adapter in extra:
        return for_platform(extra[adapter], resolved_platform)
    return RuntimeProfile(adapter, BoundaryKind.RESTRICTED_TOKEN)


def appcontainer_environment(
    profile: RuntimeProfile, *, base_env: Mapping[str, str], home: Path | None,
    tools_dir: Path | None, git_cmd_dir: Path | None, node_dir: Path | None,
    git_bash: Path | None,
) -> dict[str, str]:
    """The complete environment block for an AppContainer agent; nothing else is added.

    ``base_env`` is ``appcontainer.base_environment(...)``: its ``LOCALAPPDATA``
    comes from the known-folder API (never ``os.environ``, research Pitfall 2),
    and it is copied as is. ``PATH`` is ``tools_dir`` → Git ``cmd`` → Node →
    the base ``PATH``. The home keys are set only when the profile stages a
    home; ``CLAUDE_CODE_GIT_BASH_PATH`` only when the profile asks for it and
    Git Bash exists.
    """

    if profile.boundary is not BoundaryKind.APPCONTAINER:
        raise ValueError("appcontainer_environment needs an AppContainer runtime profile.")
    missing = [key for key in (*_BASE_KEYS, "PATH") if not base_env.get(key)]
    if missing:
        raise ValueError(f"The AppContainer base environment lacks {', '.join(missing)}.")
    env = {key: base_env[key] for key in _BASE_KEYS}
    entries = [str(entry) for entry in (tools_dir, git_cmd_dir, node_dir) if entry is not None]
    # A Windows environment block: ";" whatever the host's os.pathsep is.
    entries.extend(item for item in base_env["PATH"].split(";") if item)
    env["PATH"] = ";".join(dict.fromkeys(entries))
    if profile.staged_home:
        if home is None:
            raise ValueError("This runtime profile needs a staged home.")
        text = str(home)
        drive, rest = ntpath.splitdrive(text)
        env.update({
            "USERPROFILE": text,
            "HOME": text,
            "APPDATA": str(home / "AppData" / "Roaming"),
            "HOMEDRIVE": drive,
            "HOMEPATH": rest or "\\",
        })
    if profile.git_bash_env and git_bash is not None:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = str(git_bash)
    for key, value in profile.static_env:
        if key.upper() in PROTECTED_ENV_KEYS:
            raise ValueError(f"The runtime profile may not override the protected key {key}.")
        env[key] = value
    return env
