"""A `CredentialStorePort` kept in one owner-only file (Linux fallback store).

This is the weaker store, chosen only explicitly (see `selection.py`) and
documented as such in SECURITY.md: secrets sit unencrypted in
``<store>/credentials/credentials.json``, so any process running as the same
user can read them. OS accounts other than the owner are kept out by modes
(directory 0700, file 0600), and every read and write fails closed when those
modes or the owner are wrong instead of re-permissioning the file. That
matches the existing threat model, which already assumes same-user processes
can read the store directory and API token; the agent itself is kept out by
the launch boundary, not by this file.

Writes are atomic (temporary file in the same directory, fsync, ``os.replace``)
and serialized across processes with an exclusive ``fcntl.flock`` on a lock
file beside the data, so two backends sharing a store never lose an update.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CREDENTIAL_DIRECTORY_NAME = "credentials"
CREDENTIAL_FILENAME = "credentials.json"
LOCK_FILENAME = ".credentials.lock"
MAX_CREDENTIAL_FILE_BYTES = 4 * 1024 * 1024
SCHEMA_VERSION = 1
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class CredentialFileError(OSError):
    """The credential file or its directory is unsafe or unreadable."""


def _check_private(info: os.stat_result, path: Path, *, directory: bool) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise CredentialFileError(f"{path} is not a {'directory' if directory else 'regular file'}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise CredentialFileError(f"{path} is owned by another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise CredentialFileError(f"{path} is accessible to group or others")


class FileCredentialStore:
    """Concrete `CredentialStorePort` backed by a 0600 JSON file."""

    def __init__(self, store_directory: Path) -> None:
        if os.name == "nt":
            # POSIX modes mean nothing on Windows; Credential Manager is the store there.
            raise CredentialFileError("FileCredentialStore is not supported on Windows")
        self._directory = Path(store_directory) / CREDENTIAL_DIRECTORY_NAME
        self._path = self._directory / CREDENTIAL_FILENAME
        self._lock_path = self._directory / LOCK_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    # ---- port --------------------------------------------------------------

    def put(self, key: str, secret: str) -> None:
        self._require_key(key)
        if not isinstance(secret, str):
            raise TypeError("secret must be a string")
        with self._locked():
            secrets = self._read()
            secrets[key] = secret
            self._write(secrets)

    def get(self, key: str) -> str | None:
        self._require_key(key)
        with self._locked():
            return self._read().get(key)

    def delete(self, key: str) -> bool:
        self._require_key(key)
        with self._locked():
            secrets = self._read()
            if key not in secrets:
                return False
            del secrets[key]
            self._write(secrets)
            return True

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _require_key(key: str) -> None:
        if not isinstance(key, str) or not key or len(key) > 512 or "\0" in key:
            raise ValueError("Invalid credential key")

    def _ensure_directory(self) -> None:
        if self._directory.is_symlink():
            raise CredentialFileError(f"{self._directory} is a symlink")
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_private(self._directory.lstat(), self._directory, directory=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        self._ensure_directory()
        descriptor = os.open(self._lock_path,
                             os.O_RDWR | os.O_CREAT | _O_NOFOLLOW, 0o600)
        try:
            _check_private(os.fstat(descriptor), self._lock_path, directory=False)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _read(self) -> dict[str, str]:
        try:
            descriptor = os.open(self._path, os.O_RDONLY | _O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError as exc:  # ELOOP for a planted symlink, EACCES, ...
            raise CredentialFileError(f"{self._path} cannot be opened safely: {exc}") from exc
        try:
            info = os.fstat(descriptor)
            _check_private(info, self._path, directory=False)
            if info.st_size > MAX_CREDENTIAL_FILE_BYTES:
                raise CredentialFileError(f"{self._path} is oversized")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(MAX_CREDENTIAL_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialFileError(f"{self._path} is not valid credential JSON") from exc
        secrets = data.get("secrets") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
                or not isinstance(secrets, dict)
                or not all(isinstance(k, str) and isinstance(v, str)
                           for k, v in secrets.items())):
            raise CredentialFileError(f"{self._path} has an unexpected shape")
        return dict(secrets)

    def _write(self, secrets: dict[str, str]) -> None:
        content = json.dumps({"schema_version": SCHEMA_VERSION, "secrets": secrets},
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(content) > MAX_CREDENTIAL_FILE_BYTES:
            raise CredentialFileError("Credential file would exceed its size limit")
        # mkstemp creates the file 0600 in the private directory.
        handle, temporary = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp",
                                             dir=self._directory)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
