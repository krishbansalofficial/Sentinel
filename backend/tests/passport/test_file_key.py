"""File-backed ES256 signing (non-Windows): custody checks and verifiable output."""

from __future__ import annotations

import base64
import io
import multiprocessing
import os
import stat
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.app.passport.es256 import fingerprint, verify_signature
from backend.app.passport.file_key import (
    DEFAULT_KEY_NAME,
    FILE_KEY_LIMITATION,
    FILE_PROVIDER,
    FileSigningKey,
    _key_path,
    key_directory,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="file keys are the non-Windows signer")


def test_platform_selects_the_file_key_off_windows() -> None:
    from backend.app.passport.keys import SigningKey

    assert SigningKey is FileSigningKey


def test_create_reopen_and_sign(tmp_path: Path) -> None:
    with FileSigningKey.open(name="test key", store_directory=tmp_path) as key:
        assert key.provider == FILE_PROVIDER == "SOFTWARE_FILE"
        spki = key.public_spki()
        message = b"mock passport payload"
        signature = key.sign(message)
    assert len(signature) == 64
    assert verify_signature(spki=spki, message=message, signature=signature)
    assert not verify_signature(spki=spki, message=message + b"!", signature=signature)
    with FileSigningKey.open_existing(name="test key", store_directory=tmp_path) as again:
        assert again.public_spki() == spki
    path = _key_path(key_directory(tmp_path), "test key")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_distinct_names_are_distinct_identities(tmp_path: Path) -> None:
    prints = set()
    for index in range(5):
        with FileSigningKey.open(name=f"key {index}", store_directory=tmp_path) as key:
            prints.add(fingerprint(key.public_spki()))
    assert len(prints) == 5


def test_closed_key_refuses_to_sign(tmp_path: Path) -> None:
    key = FileSigningKey.open(name="k", store_directory=tmp_path)
    key.close()
    with pytest.raises(ValueError, match="closed"):
        key.sign(b"x")


def test_open_existing_never_creates(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="does not exist"):
        FileSigningKey.open_existing(name="absent", store_directory=tmp_path)
    assert not key_directory(tmp_path).exists()


@pytest.mark.parametrize("name", ["", "a/b", "a\\b", "x" * 201, "nul\0"])
def test_invalid_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        FileSigningKey.open(name=name, store_directory=tmp_path)


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o660])
def test_readable_key_file_fails_closed(tmp_path: Path, mode: int) -> None:
    FileSigningKey.open(name="k", store_directory=tmp_path).close()
    path = _key_path(key_directory(tmp_path), "k")
    os.chmod(path, mode)
    with pytest.raises(OSError, match="group or others"):
        FileSigningKey.open(name="k", store_directory=tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_symlinked_key_is_refused(tmp_path: Path) -> None:
    directory = key_directory(tmp_path)
    directory.mkdir(parents=True, mode=0o700)
    planted = tmp_path / "planted.pem"
    planted.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    os.chmod(planted, 0o600)
    _key_path(directory, "k").symlink_to(planted)
    with pytest.raises(OSError):
        FileSigningKey.open(name="k", store_directory=tmp_path)


def test_symlinked_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / "store").mkdir()
    key_directory(tmp_path / "store").symlink_to(elsewhere)
    with pytest.raises(OSError, match="symlink"):
        FileSigningKey.open(name="k", store_directory=tmp_path / "store")


def test_non_p256_key_is_refused(tmp_path: Path) -> None:
    directory = key_directory(tmp_path)
    directory.mkdir(parents=True, mode=0o700)
    path = _key_path(directory, "k")
    path.write_bytes(ec.generate_private_key(ec.SECP384R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="P-256"):
        FileSigningKey.open(name="k", store_directory=tmp_path)


def test_garbage_key_is_refused(tmp_path: Path) -> None:
    directory = key_directory(tmp_path)
    directory.mkdir(parents=True, mode=0o700)
    path = _key_path(directory, "k")
    path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n")
    os.chmod(path, 0o600)
    with pytest.raises(ValueError):
        FileSigningKey.open(name="k", store_directory=tmp_path)


def _open_and_report(store: str, queue) -> None:
    with FileSigningKey.open(name="raced", store_directory=Path(store)) as key:
        queue.put(fingerprint(key.public_spki()))


def test_concurrent_creation_yields_one_identity(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    processes = [context.Process(target=_open_and_report, args=(str(tmp_path), queue))
                 for _ in range(12)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
        assert process.exitcode == 0
    prints = {queue.get(timeout=5) for _ in processes}
    assert len(prints) == 1
    leftovers = [p.name for p in key_directory(tmp_path).iterdir() if p.suffix != ".pem"]
    assert leftovers == []


def test_delete_rules(tmp_path: Path) -> None:
    with FileSigningKey.open(name=DEFAULT_KEY_NAME, store_directory=tmp_path) as key:
        with pytest.raises(ValueError, match="installation key"):
            key.delete_for_test()
    with FileSigningKey.open(name="disposable", store_directory=tmp_path) as key:
        key.delete_for_test()
    assert not _key_path(key_directory(tmp_path), "disposable").exists()
    successor = f"{DEFAULT_KEY_NAME} {uuid4()}"
    FileSigningKey.open(name=successor, store_directory=tmp_path).close()
    FileSigningKey.delete_unactivated_successor(name=successor, store_directory=tmp_path)
    assert not _key_path(key_directory(tmp_path), successor).exists()
    for bad in ("other", f"{DEFAULT_KEY_NAME} not-a-uuid",
                f"{DEFAULT_KEY_NAME} 00000000-0000-1000-8000-000000000000"):
        with pytest.raises(ValueError):
            FileSigningKey.delete_unactivated_successor(name=bad, store_directory=tmp_path)


# ---- end to end: issue and verify a Passport v2 and a bundle on this platform


def test_v2_issue_is_signed_and_labelled_software_file(tmp_path: Path) -> None:
    from backend.app.core.journal import JournalWriter
    from backend.app.contracts.models import JournalEventType
    from backend.app.passport.v2 import PassportV2Issuer, canonical_payload
    from backend.tests.passport.test_builder import _database, _seed_change
    from backend.tests.passport.test_v2 import _launch

    database = _database(tmp_path)
    change = _seed_change(database)
    JournalWriter(database).append(change.id, JournalEventType.PASSPORT_BUILT,
                                   payload={"source": "mock"})
    run_id = _launch(database, change.id)
    issued = PassportV2Issuer(database, key_name=f"disposable {uuid4()}",
                              installation_label="Linux lab").issue(change.id)
    assert issued.signer_provider == "SOFTWARE_FILE"
    assert issued.payload.signer_provider == "SOFTWARE_FILE"
    assert FILE_KEY_LIMITATION in issued.payload.limitations
    assert [str(item.run_id) for item in issued.payload.launch_records] == [run_id]
    assert issued.payload.execution_boundary == "UNKNOWN"
    spki = base64.b64decode(issued.signer_public_spki_b64)
    assert verify_signature(spki=spki, message=canonical_payload(issued.payload),
                            signature=base64.b64decode(issued.signature_b64))


def test_bundle_export_verifies_offline_with_the_file_key(tmp_path: Path) -> None:
    from backend.app.passport.bundle import BundleExporter
    from backend.app.passport.jcs import parse_canonical
    from backend.app.passport.trust import TrustRegistry
    from backend.app.passport.verify import verify_bundle
    from backend.tests.passport.test_builder import _database, _seed_change

    database = _database(tmp_path)
    change = _seed_change(database)
    artifact = BundleExporter(database, key_name=f"disposable {uuid4()}").export(change.id)
    path = tmp_path / artifact.filename
    path.write_bytes(artifact.content)
    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
        passport = parse_canonical(archive.read("passport.json"))
        signature = parse_canonical(archive.read("signature.json"))
    assert signature["provider"] == "SOFTWARE_FILE"
    assert FILE_KEY_LIMITATION in passport["claims"]["limitations"]
    trust = TrustRegistry(tmp_path / "trusted_keys.json")
    untrusted = verify_bundle(path, trust=trust)
    assert untrusted.verdict != "VALID"
    trust.add(spki=base64.b64decode(signature["public_spki_b64"]), label="Linux lab")
    verdict = verify_bundle(path, trust=trust)
    assert verdict.verdict == "VALID", verdict.reason
    # A single flipped byte in the signed claims breaks verification.
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(tampered, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "passport.json":
                data = data.replace(b"Linux", b"Linuz") if b"Linux" in data else data.replace(
                    b'"UNKNOWN"', b'"PASS"', 1)
            target.writestr(item, data)
    assert verify_bundle(tampered, trust=trust).verdict != "VALID"
