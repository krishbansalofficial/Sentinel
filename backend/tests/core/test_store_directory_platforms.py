"""The default store directory follows each platform's convention."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from backend.app.core.evidence_store import (
    DATABASE_FILENAME,
    default_database_path,
    default_store_directory,
    prepare_store_directory,
)


def test_windows_path_is_unchanged(tmp_path: Path) -> None:
    environ = {"LOCALAPPDATA": str(tmp_path / "Local")}
    assert default_store_directory(environ, platform="win32") == tmp_path / "Local" / "Sentinel"
    assert default_store_directory({}, platform="win32") == (
        Path.home() / "AppData" / "Local" / "Sentinel")


def test_windows_ignores_xdg(tmp_path: Path) -> None:
    environ = {"LOCALAPPDATA": str(tmp_path / "L"), "XDG_DATA_HOME": str(tmp_path / "X")}
    assert default_store_directory(environ, platform="win32") == tmp_path / "L" / "Sentinel"


def test_macos_uses_application_support(tmp_path: Path) -> None:
    environ = {"LOCALAPPDATA": str(tmp_path), "XDG_DATA_HOME": str(tmp_path)}
    assert default_store_directory(environ, platform="darwin") == (
        Path.home() / "Library" / "Application Support" / "Sentinel")


def test_linux_uses_xdg_data_home(tmp_path: Path) -> None:
    environ = {"XDG_DATA_HOME": str(tmp_path / "data"), "LOCALAPPDATA": "ignored"}
    assert default_store_directory(environ, platform="linux") == tmp_path / "data" / "sentinel"


@pytest.mark.parametrize("value", ["", "relative/data", "./data"])
def test_linux_ignores_unset_or_relative_xdg(value: str) -> None:
    environ = {"XDG_DATA_HOME": value} if value else {}
    assert default_store_directory(environ, platform="linux") == (
        Path.home() / ".local" / "share" / "sentinel")


def test_other_posix_follows_linux(tmp_path: Path) -> None:
    environ = {"XDG_DATA_HOME": str(tmp_path)}
    assert default_store_directory(environ, platform="freebsd14") == tmp_path / "sentinel"


def test_database_path_follows_directory(tmp_path: Path) -> None:
    environ = {"XDG_DATA_HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path)}
    assert default_database_path(environ).name == DATABASE_FILENAME
    assert default_database_path(environ).parent == default_store_directory(environ)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_lowercase_xdg_directory_is_prepared_private(tmp_path: Path) -> None:
    directory = default_store_directory({"XDG_DATA_HOME": str(tmp_path)}, platform="linux")
    assert prepare_store_directory(directory) is True
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_xdg_store_is_refused(tmp_path: Path) -> None:
    from backend.app.core.errors import AppError

    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sentinel").symlink_to(real)
    with pytest.raises(AppError):
        prepare_store_directory(tmp_path / "data" / "sentinel")


@pytest.mark.skipif(os.name == "nt", reason="the non-Windows locations")
def test_trust_registry_and_signer_selector_follow_the_store(tmp_path: Path, monkeypatch) -> None:
    from backend.app.passport.identity import identity_path
    from backend.app.passport.trust import default_trust_path

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_trust_path() == tmp_path / "sentinel" / "trusted_keys.json"
    assert identity_path() == tmp_path / "sentinel" / "signing_identity.json"
