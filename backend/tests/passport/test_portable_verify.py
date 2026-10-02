"""Pinned-only verification runs anywhere: no registry, no Windows APIs, no Typer."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from backend.app.passport import es256
from backend.app.passport.jcs import parse_canonical
from backend.app.passport.portable_verify import main as portable_main
from backend.app.passport.verify import verify_bundle

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = Path(__file__).parent / "fixtures" / "golden-v2.sentinel"
GOLDEN_CHANGE = "cd12e3f4-2eed-4bf3-8505-ecc145cb4db5"
OTHER_FP = "-".join(["A" * 8] * 6 + ["AAAA"])
BLOCKED = (
    "ctypes.wintypes", "winreg", "msvcrt",
    "backend.app.passport.cng", "backend.app.passport.trust",
    "backend.app.passport.identity", "backend.app.core.evidence_store",
    "typer", "click",
)


def _golden_fp() -> str:
    with zipfile.ZipFile(GOLDEN) as archive:
        return parse_canonical(archive.read("signature.json"))["fingerprint"]


def _tampered(tmp_path: Path) -> Path:
    raw = bytearray(GOLDEN.read_bytes())
    marker = raw.find(b"Golden fixture")
    assert marker > 0
    raw[marker] ^= 0x01
    target = tmp_path / "tampered.sentinel"
    target.write_bytes(bytes(raw))
    return target


def _p256_spki(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def _raw_sign(key: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
    r, s = utils.decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# --- es256 primitives -------------------------------------------------------

def test_es256_fingerprint_is_grouped_base32_and_normalizes_to_itself() -> None:
    spki = _p256_spki(ec.generate_private_key(ec.SECP256R1()))
    value = es256.fingerprint(spki)
    assert [len(group) for group in value.split("-")] == [8] * 6 + [4]
    assert es256.normalize_fingerprint(value) == value
    assert es256.normalize_fingerprint(value.replace("-", " ").lower()) == value


def test_es256_fingerprint_rejects_non_p256_keys() -> None:
    spki = _p256_spki(ec.generate_private_key(ec.SECP384R1()))
    with pytest.raises(ValueError):
        es256.fingerprint(spki)


@pytest.mark.parametrize("value", ["", "ABC", "1" * 52, "A" * 51, "A" * 53])
def test_es256_normalize_rejects_malformed_fingerprints(value: str) -> None:
    with pytest.raises(ValueError):
        es256.normalize_fingerprint(value)


def test_es256_verify_signature_accepts_only_the_signed_message() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    spki, signature = _p256_spki(key), _raw_sign(key, b"manifest")
    assert es256.verify_signature(spki=spki, message=b"manifest", signature=signature)
    assert not es256.verify_signature(spki=spki, message=b"manifesT", signature=signature)
    assert not es256.verify_signature(spki=spki, message=b"manifest", signature=signature[:-1])
    other = _p256_spki(ec.generate_private_key(ec.SECP256R1()))
    assert not es256.verify_signature(spki=other, message=b"manifest", signature=signature)
    p384 = _p256_spki(ec.generate_private_key(ec.SECP384R1()))
    assert not es256.verify_signature(spki=p384, message=b"manifest", signature=signature)
    assert not es256.verify_signature(spki=b"not der", message=b"manifest", signature=signature)


def test_cng_and_trust_reexport_the_portable_primitives() -> None:
    from backend.app.passport import cng, trust
    assert cng.fingerprint is es256.fingerprint
    assert cng.verify_signature is es256.verify_signature
    assert trust.normalize_fingerprint is es256.normalize_fingerprint


# --- pinned-only verify_bundle ----------------------------------------------

def test_pinned_only_valid_with_matching_pin_and_reads_no_registry(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    class Exploding:
        def decision(self, **_: object) -> object:
            raise AssertionError("pinned-only mode must not consult a registry")

    pin = _golden_fp()
    result = verify_bundle(GOLDEN, trust=Exploding(),  # type: ignore[arg-type]
                           expected_fingerprint=pin.lower().replace("-", ""),
                           use_registry=False)
    assert result.verdict == "VALID", result.reason
    assert result.signer_fingerprint == pin
    assert result.change_id == GOLDEN_CHANGE
    assert result.trusted_as is None


@pytest.mark.parametrize(("pin", "reason"), [
    (OTHER_FP, "does not match"),
    (None, "No pinned fingerprint"),
    ("not-a-fingerprint", "malformed"),
])
def test_pinned_only_wrong_missing_or_malformed_pin_is_indeterminate(
        pin: str | None, reason: str) -> None:
    result = verify_bundle(GOLDEN, expected_fingerprint=pin, use_registry=False)
    assert result.verdict == "INDETERMINATE"
    assert reason in result.reason
    assert result.signer_fingerprint == _golden_fp()


def test_pinned_only_tampered_bundle_is_invalid_even_with_matching_pin(tmp_path: Path) -> None:
    result = verify_bundle(_tampered(tmp_path), expected_fingerprint=_golden_fp(),
                           use_registry=False)
    assert result.verdict == "INVALID"


# --- portable CLI ------------------------------------------------------------

def _run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = portable_main(list(argv))
    return code, capsys.readouterr().out


def test_cli_valid_exits_zero_with_sentinel_verify_json_shape(
        capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run_cli(capsys, str(GOLDEN), "--fingerprint", _golden_fp(), "--json")
    assert code == 0
    assert "\n" not in out.strip()
    payload = json.loads(out)
    assert set(payload) == {"verdict", "reason", "change_id", "payload_sha256",
                            "signer_fingerprint", "signer_identity", "trusted_as", "claims"}
    assert payload["verdict"] == "VALID"
    assert payload["change_id"] == GOLDEN_CHANGE


def test_cli_human_output_is_indented_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run_cli(capsys, str(GOLDEN), "--fingerprint", _golden_fp())
    assert code == 0
    assert out.startswith("{\n  ")
    assert json.loads(out)["verdict"] == "VALID"


def test_cli_tampered_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run_cli(capsys, str(_tampered(tmp_path)), "--fingerprint", _golden_fp(),
                         "--json")
    assert code == 1
    assert json.loads(out)["verdict"] == "INVALID"


def test_cli_wrong_pin_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run_cli(capsys, str(GOLDEN), "--fingerprint", OTHER_FP, "--json")
    assert code == 2
    assert json.loads(out)["verdict"] == "INDETERMINATE"


@pytest.mark.parametrize("argv", [
    [],
    [str(GOLDEN)],
    ["missing.sentinel", "--fingerprint", OTHER_FP],
    [str(GOLDEN), "--fingerprint", "bogus"],
    [str(GOLDEN), "--fingerprint", OTHER_FP, "--unknown"],
])
def test_cli_usage_errors_exit_three_not_argparse_two(
        argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run_cli(capsys, *argv, "--json")
    assert code == 3
    payload = json.loads(out)
    assert payload["verdict"] == "USAGE_ERROR"
    assert payload["reason"]


def test_cli_matches_sentinel_verify_exit_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    from typer.testing import CliRunner
    from backend.app.cli.main import app as cli_app

    pin = _golden_fp()
    sentinel = CliRunner().invoke(cli_app, ["verify", str(GOLDEN), "--key", pin, "--json"])
    code, out = _run_cli(capsys, str(GOLDEN), "--fingerprint", pin, "--json")
    ours, theirs = json.loads(out), json.loads(sentinel.output)
    assert code == sentinel.exit_code
    assert set(ours) == set(theirs)
    for key in ("verdict", "change_id", "payload_sha256", "signer_fingerprint",
                "signer_identity", "claims"):
        assert ours[key] == theirs[key], key


# --- GitHub Action -------------------------------------------------------------

def test_action_installs_exact_pyproject_pins_and_runs_the_portable_module() -> None:
    import re
    import tomllib

    action = (REPO_ROOT / ".github" / "actions" / "verify-passport" / "action.yml").read_text(
        encoding="utf-8")
    dependencies = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["dependencies"]
    for name in ("cryptography", "pydantic"):
        pins = [dep for dep in dependencies if re.match(rf"{name}==", dep)]
        assert len(pins) == 1, name
        assert f'"{pins[0]}"' in action, pins[0]
    installs = re.findall(r'"([a-z0-9_-]+)==[^"]+"', action)
    assert sorted(installs) == ["cryptography", "pydantic"]
    assert "-m backend.app.passport.portable_verify" in action
    assert "${{ github.action_path }}/../../.." in action
    # Inputs reach the shell only through env, never interpolated into the script.
    script = action.split("run: |", 1)[1]
    assert "${{" not in script


# --- import isolation ----------------------------------------------------------

def test_verifier_imports_and_verifies_with_windows_modules_unimportable() -> None:
    """Simulates a Linux CI runner: Windows-only and registry modules cannot load."""
    bootstrap = f"""
import sys
for name in {BLOCKED!r}:
    sys.modules[name] = None
import importlib
for name in {BLOCKED!r}:
    try:
        importlib.import_module(name)
    except ImportError:
        pass
    else:
        raise SystemExit("block failed: " + name)
from pathlib import Path
from backend.app.passport.verify import verify_bundle
result = verify_bundle(Path({str(GOLDEN)!r}), expected_fingerprint={_golden_fp()!r},
                       use_registry=False)
assert result.verdict == "VALID", result.reason
registry = verify_bundle(Path({str(GOLDEN)!r}))
assert registry.verdict == "INDETERMINATE", registry
from backend.app.passport.portable_verify import main
code = main([{str(GOLDEN)!r}, "--fingerprint", {_golden_fp()!r}, "--json"])
assert code == 0, code
loaded = sorted(n for n in sys.modules if n.startswith("backend.") and sys.modules[n] is not None)
print("LOADED=" + ",".join(loaded))
"""
    completed = subprocess.run([sys.executable, "-c", bootstrap], cwd=REPO_ROOT,
                               capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert '"verdict":"VALID"' in completed.stdout
    loaded = completed.stdout.rsplit("LOADED=", 1)[1].strip().split(",")
    assert "backend.app.passport.verify" in loaded
    assert not {"backend.app.passport.cng", "backend.app.passport.trust",
                "backend.app.core.evidence_store"} & set(loaded)
