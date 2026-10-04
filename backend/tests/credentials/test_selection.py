"""Credential store selection is explicit per platform and never falls back."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from backend.app.credentials import selection
from backend.app.credentials.selection import (
    CREDENTIAL_STORE_ENV,
    CredentialStoreSelectionError,
    choose_credential_store_kind,
    select_credential_store,
)
from backend.app.credentials.keyring_store import KeyringUnavailableError
from backend.tests.credentials.test_keyring_store import fake_keyring


@pytest.mark.parametrize(("platform", "expected"), [
    ("win32", "windows"),
    ("darwin", "keyring"),
    ("linux", "file"),
    ("freebsd14", "file"),
])
def test_platform_defaults(platform: str, expected: str) -> None:
    assert choose_credential_store_kind(platform, {}) == expected


@pytest.mark.parametrize(("platform", "requested", "expected"), [
    ("linux", "keyring", "keyring"),
    ("linux", " KEYRING ", "keyring"),
    ("darwin", "file", "file"),
    ("win32", "keyring", "keyring"),
    ("win32", "windows", "windows"),
    ("linux", "", "file"),
])
def test_override(platform: str, requested: str, expected: str) -> None:
    assert choose_credential_store_kind(platform, {CREDENTIAL_STORE_ENV: requested}) == expected


@pytest.mark.parametrize(("platform", "requested", "match"), [
    ("linux", "windows", "only available on Windows"),
    ("darwin", "windows", "only available on Windows"),
    ("win32", "file", "not available on Windows"),
    ("linux", "plaintext", "not one of"),
    ("linux", "memory", "not one of"),
])
def test_impossible_or_unknown_requests_refuse(platform: str, requested: str, match: str) -> None:
    with pytest.raises(CredentialStoreSelectionError, match=match):
        choose_credential_store_kind(platform, {CREDENTIAL_STORE_ENV: requested})


@pytest.mark.skipif(os.name == "nt", reason="file store is POSIX-only")
def test_file_store_is_announced_as_weaker(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="test.selection"):
        selected = select_credential_store(tmp_path, logger=logging.getLogger("test.selection"),
                                           platform="linux", environ={})
    assert selected.kind == "file" and selected.weaker
    selected.store.put("k", "mock")
    assert selected.store.get("k") == "mock"
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "Credential store: file" in record.getMessage()
    assert "SENTINEL_CREDENTIAL_STORE=keyring" in record.getMessage()


def test_keyring_store_is_announced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring())
    with caplog.at_level(logging.INFO, logger="test.selection"):
        selected = select_credential_store(tmp_path, logger=logging.getLogger("test.selection"),
                                           platform="darwin", environ={})
    assert selected.kind == "keyring" and not selected.weaker
    [record] = caplog.records
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith("Credential store: keyring (OS keyring (")


def test_unusable_keyring_never_falls_back_to_the_file_store(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(KeyringUnavailableError):
        select_credential_store(tmp_path, logger=logging.getLogger("test"),
                                platform="darwin", environ={})
    assert not (tmp_path / "credentials").exists()


def test_create_app_refuses_startup_when_the_store_cannot_open(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.core.config import Settings
    from backend.app.main import create_app

    monkeypatch.setenv(CREDENTIAL_STORE_ENV, "plaintext")
    settings = Settings(database_path=tmp_path / "store" / "db.sqlite3")
    with pytest.raises(CredentialStoreSelectionError):
        create_app(settings=settings)


@pytest.mark.skipif(os.name == "nt", reason="file store is POSIX-only")
def test_create_app_uses_the_file_store_beside_the_database(tmp_path: Path) -> None:
    from backend.app.core.config import Settings
    from backend.app.credentials.file_store import FileCredentialStore
    from backend.app.main import create_app

    app = create_app(settings=Settings(database_path=tmp_path / "store" / "db.sqlite3"))
    assert app.state.credential_store_kind == "file"
    broker_store = app.state.credential_broker.store
    assert isinstance(broker_store, FileCredentialStore)
    assert broker_store.path == tmp_path / "store" / "credentials" / "credentials.json"


def test_selection_module_has_no_silent_fallback_branch() -> None:
    # A guard against a future "try keyring, except: use file" edit.
    source = Path(selection.__file__).read_text(encoding="utf-8")
    assert "except" not in source
