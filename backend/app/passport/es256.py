"""Portable ES256 primitives for Passport verification (no Windows APIs).

Signing stays in ``cng`` (Windows CNG keys); everything a recipient needs to
check a bundle lives here so the verifier imports on any platform.
"""

from __future__ import annotations

import base64
import hashlib
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

_FINGERPRINT = re.compile(r"^[A-Z2-7]{52}$")


def fingerprint(spki: bytes) -> str:
    """Grouped base32 SHA-256 of the canonical DER SubjectPublicKeyInfo."""
    public = serialization.load_der_public_key(spki)
    if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(public.curve, ec.SECP256R1):
        raise ValueError("Fingerprint requires an ES256 SPKI")
    canonical = public.public_bytes(serialization.Encoding.DER,
                                    serialization.PublicFormat.SubjectPublicKeyInfo)
    encoded = base64.b32encode(hashlib.sha256(canonical).digest()).decode("ascii").rstrip("=")
    return "-".join(encoded[index:index + 8] for index in range(0, len(encoded), 8))


def verify_signature(*, spki: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an ES256 raw signature using only the public SPKI."""
    try:
        public = serialization.load_der_public_key(spki)
        if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(public.curve, ec.SECP256R1):
            return False
        if len(signature) != 64:
            return False
        der = utils.encode_dss_signature(int.from_bytes(signature[:32], "big"),
                                         int.from_bytes(signature[32:], "big"))
        public.verify(der, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, InvalidSignature):
        return False


def normalize_fingerprint(value: str) -> str:
    compact = value.replace("-", "").replace(" ", "").upper()
    if not _FINGERPRINT.fullmatch(compact):
        raise ValueError("Invalid grouped base32 SHA-256 fingerprint")
    return "-".join(compact[index:index + 8] for index in range(0, 52, 8))
