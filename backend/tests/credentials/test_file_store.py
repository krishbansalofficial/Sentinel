"""The owner-only file credential store: round trips, fail-closed modes, races.

All secrets here are generated mock data; nothing touches a real keyring.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import random
import stat
import string
from pathlib import Path

import pytest

from backend.app.credentials.file_store import (
    CREDENTIAL_DIRECTORY_NAME,
    CREDENTIAL_FILENAME,
    CredentialFileError,
    FileCredentialStore,
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics only")


def _mock_secret(rng: random.Random, length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.~ ü€"
    return "".join(rng.choice(alphabet) for _ in range(length))


@pytest.mark.skipif(os.name != "nt", reason="Windows refusal only")
def test_refused_on_windows(tmp_path: Path) -> None:
    with pytest.raises(CredentialFileError, match="not supported on Windows"):
        FileCredentialStore(tmp_path)


@posix_only
def test_round_trip_and_missing_keys(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    assert store.get("github-token") is None
    assert store.delete("github-token") is False
    store.put("github-token", "ghp_mock_0123456789")
    store.put("empty", "")
    assert store.get("github-token") == "ghp_mock_0123456789"
    assert store.get("empty") == ""
    store.put("github-token", "ghp_mock_rotated")
    assert store.get("github-token") == "ghp_mock_rotated"
    assert store.delete("github-token") is True
    assert store.get("github-token") is None
    assert store.get("empty") == ""


@posix_only
def test_persists_across_instances_with_private_modes(tmp_path: Path) -> None:
    rng = random.Random(1234)
    expected = {f"mock-key-{index}": _mock_secret(rng) for index in range(50)}
    writer = FileCredentialStore(tmp_path)
    for key, secret in expected.items():
        writer.put(key, secret)
    reader = FileCredentialStore(tmp_path)
    assert {key: reader.get(key) for key in expected} == expected
    directory = tmp_path / CREDENTIAL_DIRECTORY_NAME
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / CREDENTIAL_FILENAME).stat().st_mode) == 0o600
    # No temporary files are left behind by the atomic writes.
    assert sorted(path.name for path in directory.iterdir()) == [
        ".credentials.lock", CREDENTIAL_FILENAME]


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o666])
def test_group_or_world_accessible_file_fails_closed(tmp_path: Path, mode: int) -> None:
    store = FileCredentialStore(tmp_path)
    store.put("k", "mock")
    path = tmp_path / CREDENTIAL_DIRECTORY_NAME / CREDENTIAL_FILENAME
    os.chmod(path, mode)
    with pytest.raises(CredentialFileError, match="group or others"):
        store.get("k")
    with pytest.raises(CredentialFileError):
        store.put("k2", "mock")
    assert stat.S_IMODE(path.stat().st_mode) == mode  # never silently re-permissioned


@posix_only
def test_open_directory_fails_closed(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    store.put("k", "mock")
    os.chmod(tmp_path / CREDENTIAL_DIRECTORY_NAME, 0o755)
    with pytest.raises(CredentialFileError, match="group or others"):
        store.get("k")


@posix_only
def test_symlinked_credential_file_is_refused(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    store.put("k", "mock")
    directory = tmp_path / CREDENTIAL_DIRECTORY_NAME
    decoy = tmp_path / "decoy.json"
    decoy.write_text(json.dumps({"schema_version": 1, "secrets": {"k": "planted"}}))
    os.chmod(decoy, 0o600)
    (directory / CREDENTIAL_FILENAME).unlink()
    (directory / CREDENTIAL_FILENAME).symlink_to(decoy)
    with pytest.raises(CredentialFileError, match="cannot be opened safely"):
        store.get("k")


@posix_only
def test_symlinked_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir(mode=0o700)
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / CREDENTIAL_DIRECTORY_NAME).symlink_to(real)
    with pytest.raises(CredentialFileError, match="symlink"):
        FileCredentialStore(tmp_path / "store").put("k", "mock")


@posix_only
@pytest.mark.parametrize("content", [
    b"not json",
    b"\xff\xfe",
    b"[]",
    b'{"schema_version": 2, "secrets": {}}',
    b'{"schema_version": 1, "secrets": []}',
    b'{"schema_version": 1, "secrets": {"k": 5}}',
    b'{"schema_version": 1}',
])
def test_malformed_file_fails_closed(tmp_path: Path, content: bytes) -> None:
    store = FileCredentialStore(tmp_path)
    store.put("k", "mock")
    path = tmp_path / CREDENTIAL_DIRECTORY_NAME / CREDENTIAL_FILENAME
    path.write_bytes(content)
    with pytest.raises(CredentialFileError):
        store.get("k")


@posix_only
@pytest.mark.parametrize("key", ["", "a\0b", "x" * 513])
def test_invalid_keys_are_rejected(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError):
        FileCredentialStore(tmp_path).put(key, "mock")


def _hammer(store_directory: str, worker: int, count: int) -> None:
    store = FileCredentialStore(Path(store_directory))
    for index in range(count):
        store.put(f"worker-{worker}-key-{index}", f"secret-{worker}-{index}")
        if index % 5 == 0:
            store.delete(f"worker-{worker}-key-{index}")


@posix_only
def test_concurrent_processes_never_lose_an_update(tmp_path: Path) -> None:
    workers, count = 8, 25
    context = multiprocessing.get_context("fork")
    processes = [context.Process(target=_hammer, args=(str(tmp_path), worker, count))
                 for worker in range(workers)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
        assert process.exitcode == 0
    store = FileCredentialStore(tmp_path)
    for worker in range(workers):
        for index in range(count):
            value = store.get(f"worker-{worker}-key-{index}")
            if index % 5 == 0:
                assert value is None
            else:
                assert value == f"secret-{worker}-{index}"
