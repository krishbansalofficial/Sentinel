"""The Passport signing key class for this platform, chosen explicitly.

Windows signs through CNG (`cng.CngKey`: TPM, else the non-exportable Software
KSP). Every other platform signs with `file_key.FileSigningKey`, a 0600 PKCS#8
file reported as ``SOFTWARE_FILE``. This is a declared per-platform choice, not
a fallback: a CNG failure on Windows is never retried with a file key.
"""

from __future__ import annotations

import os

from backend.app.passport.file_key import FILE_KEY_LIMITATION, FILE_PROVIDER, FileSigningKey

if os.name == "nt":
    from backend.app.passport.cng import CngKey as SigningKey
else:
    SigningKey = FileSigningKey  # type: ignore[assignment,misc]

__all__ = ["FILE_KEY_LIMITATION", "FILE_PROVIDER", "SigningKey"]
