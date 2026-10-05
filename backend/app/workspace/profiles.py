"""Where a workspace's per-Change isolation identity and storage come from.

Windows: an AppContainer profile (package SID, ``%LOCALAPPDATA%\\Packages\\<name>\\AC``),
exactly as before. Linux: a private directory ``<store>/linux-workspaces/<name>/AC``
(mode 0700) with the identifier ``linux-sandbox:<name>``. The Linux identity is
not a security principal: the sandbox boundary comes from bubblewrap, seccomp
and the run cgroup at launch (`execution.linux_sandbox`). It only names the
workspace's storage, so the identifier carries a prefix that can never be
mistaken for a Windows SID (``S-1-15-2-...``).

The composition root chooses one per platform explicitly (`main.create_app`).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from backend.app.execution import appcontainer

LINUX_IDENTITY_PREFIX = "linux-sandbox:"
LINUX_WORKSPACES_DIRECTORY = "linux-workspaces"


@runtime_checkable
class ContainerProfiles(Protocol):
    def ensure(self, name: str) -> tuple[str, Path]:
        """Create (or reuse) ``name``; returns (identity, container folder)."""

    def delete(self, name: str) -> None:
        """Delete ``name``'s identity (absent counts as success)."""

    def exists(self, name: str) -> bool:
        """Whether ``name``'s identity still exists."""

    def storage_root(self, name: str) -> Path:
        """The folder that holds ``name``'s container folder (removed on cleanup)."""


class LinuxWorkspaceProfiles:
    """Private per-workspace directories under the store (Linux sandbox storage)."""

    def __init__(self, store_directory: Path) -> None:
        self._root = Path(store_directory) / LINUX_WORKSPACES_DIRECTORY

    def _checked_root(self) -> Path:
        if self._root.is_symlink():
            raise OSError(f"{self._root} is a symlink")
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self._root.lstat()
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
            raise OSError(f"{self._root} is not a directory owned by this user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(self._root, 0o700)
        return self._root

    def ensure(self, name: str) -> tuple[str, Path]:
        appcontainer.validate_profile_name(name)
        storage = self._checked_root() / name
        if storage.is_symlink():
            raise OSError(f"{storage} is a symlink")
        container = storage / "AC"
        container.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(storage, 0o700)
        return LINUX_IDENTITY_PREFIX + name, container

    def delete(self, name: str) -> None:
        appcontainer.validate_profile_name(name)  # storage is removed by the manager

    def exists(self, name: str) -> bool:
        return os.path.lexists(self._root / name)

    def storage_root(self, name: str) -> Path:
        appcontainer.validate_profile_name(name)
        return self._root / name
