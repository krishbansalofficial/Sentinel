"""File-backed ES256 signing for platforms without Windows CNG (Linux, macOS).

Weaker key custody than CNG, and reported as such: the private key is a PKCS#8
PEM file that any process running as the same user can read. Bundles signed
with it carry ``signer_provider = "SOFTWARE_FILE"`` (never ``SOFTWARE``, which
names the Windows Software KSP whose keys are non-exportable through CNG) and a
limitation line saying so. Verification is unchanged: it only needs the public
SPKI, which `es256` handles on every platform.

Layout: ``<store>/signing-keys/<sha256(name)>.pem``, the directory mode 0700 and
each key 0600. Opening a key fails closed when the file is a symlink, is not a
regular file, is owned by another user, or is readable or writable by group or
others; it is never silently re-permissioned. Creation is atomic and never
replaces an existing key: the key is written to a private temporary file and
published with ``os.link``, which fails if another process created it first,
in which case that key is used.

The API mirrors `cng.CngKey` (``open``, ``open_existing``, ``public_spki``,
``sign``, ``close``, ``delete_for_test``, ``delete_unactivated_successor``) so
the Passport issuers stay platform-neutral; `keys.SigningKey` picks one.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from backend.app.passport.es256 import fingerprint, verify_signature  # noqa: F401

FILE_PROVIDER = "SOFTWARE_FILE"
DEFAULT_KEY_NAME = "Sentinel Passport v2 ES256"
KEY_DIRECTORY_NAME = "signing-keys"
MAX_KEY_FILE_BYTES = 16 * 1024
FILE_KEY_LIMITATION = (
    "Software file key (PKCS#8, mode 0600) is readable by any same-user process; "
    "no hardware or OS key isolation."
)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def key_directory(store_directory: Path | None = None) -> Path:
    if store_directory is None:
        from backend.app.core.evidence_store import default_store_directory

        store_directory = default_store_directory()
    return Path(store_directory) / KEY_DIRECTORY_NAME


def _validate_name(name: str) -> None:
    if not name or len(name) > 200 or "\\" in name or "/" in name or "\0" in name:
        raise ValueError("Invalid signing key name")


def _key_path(directory: Path, name: str) -> Path:
    return directory / f"{hashlib.sha256(name.encode('utf-8')).hexdigest()}.pem"


def _prepare_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise OSError(f"Signing key directory {directory} is a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"Signing key directory {directory} is not a directory")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise OSError(f"Signing key directory {directory} is owned by another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(directory, 0o700)  # our own directory: tighten, then re-check
        if stat.S_IMODE(directory.lstat().st_mode) & 0o077:
            raise OSError(f"Signing key directory {directory} cannot be made private")


def _read_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """Read a key file, refusing anything that is not a private regular file."""
    descriptor = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"Signing key {path} is not a regular file")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise OSError(f"Signing key {path} is owned by another user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise OSError(f"Signing key {path} is accessible to group or others")
        if info.st_size > MAX_KEY_FILE_BYTES:
            raise ValueError(f"Signing key {path} is oversized")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_KEY_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError(f"Signing key {path} is not a P-256 key")
    return key


def _publish_new_key(directory: Path, path: Path) -> None:
    """Create a key at ``path`` unless one already exists (never replaces)."""
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    # mkstemp creates the file 0600, so the key is never briefly readable by others.
    handle, temporary = tempfile.mkstemp(prefix=".key-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(pem)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # a concurrent creator won; its key is the identity
    finally:
        os.unlink(temporary)


@dataclass(slots=True)
class FileSigningKey:
    """An opened file-backed P-256 key; use as a context manager."""

    _private_key: ec.EllipticCurvePrivateKey | None
    _path: Path
    provider: str
    name: str

    @classmethod
    def open(cls, *, name: str = DEFAULT_KEY_NAME,
             store_directory: Path | None = None) -> FileSigningKey:
        _validate_name(name)
        directory = key_directory(store_directory)
        _prepare_directory(directory)
        path = _key_path(directory, name)
        if not os.path.lexists(path):
            _publish_new_key(directory, path)
        return cls(_read_private_key(path), path, FILE_PROVIDER, name)

    @classmethod
    def open_existing(cls, *, name: str,
                      store_directory: Path | None = None) -> FileSigningKey:
        """Open a selected identity without ever creating a replacement."""
        _validate_name(name)
        directory = key_directory(store_directory)
        path = _key_path(directory, name)
        if not os.path.lexists(path):
            raise OSError("Selected file signing identity does not exist")
        _prepare_directory(directory)
        return cls(_read_private_key(path), path, FILE_PROVIDER, name)

    def _key(self) -> ec.EllipticCurvePrivateKey:
        if self._private_key is None:
            raise ValueError("Signing key is closed")
        return self._private_key

    def public_spki(self) -> bytes:
        return self._key().public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(self, message: bytes) -> bytes:
        der = self._key().sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")  # JOSE ES256: r || s

    def close(self) -> None:
        self._private_key = None

    def delete_for_test(self) -> None:
        """Remove a disposable test key, never the installation's default key."""
        if self.name == DEFAULT_KEY_NAME:
            raise ValueError("Cannot delete the installation key")
        self._path.unlink()
        self.close()

    @classmethod
    def delete_unactivated_successor(cls, *, name: str,
                                     store_directory: Path | None = None) -> None:
        """Delete a new rotation key (only generated successor names qualify)."""
        prefix = f"{DEFAULT_KEY_NAME} "
        if not name.startswith(prefix):
            raise ValueError("Only generated rotation successors may be deleted")
        try:
            suffix = name[len(prefix):]
            parsed = UUID(suffix)
            if str(parsed) != suffix or parsed.version != 4:
                raise ValueError("Invalid successor name")
        except ValueError as exc:
            raise ValueError("Invalid successor name") from exc
        _key_path(key_directory(store_directory), name).unlink(missing_ok=True)

    def __enter__(self) -> FileSigningKey:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
