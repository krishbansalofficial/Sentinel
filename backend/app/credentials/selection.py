"""Which `CredentialStorePort` the composition root builds, chosen explicitly.

The choice is a declared property of the platform plus an optional operator
override, never a fallback: a store that fails to open is an error, and no
weaker store is tried in its place (the same rule D-01 sets for launch
boundaries).

=========  ===========================  ==========================================
kind       store                        default on
=========  ===========================  ==========================================
windows    Windows Credential Manager   win32
keyring    OS keyring via `keyring`     darwin (Keychain); opt-in on Linux
file       0600 JSON file in the store  Linux and other POSIX (weaker, SECURITY.md)
=========  ===========================  ==========================================

``SENTINEL_CREDENTIAL_STORE`` overrides the default with one of those kinds. An
unknown value, or a kind the platform cannot provide (``windows`` off Windows,
``file`` on Windows), refuses startup.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from backend.app.contracts.ports import CredentialStorePort

CredentialStoreKind = Literal["windows", "keyring", "file"]
CREDENTIAL_STORE_ENV = "SENTINEL_CREDENTIAL_STORE"
_KINDS: tuple[CredentialStoreKind, ...] = ("windows", "keyring", "file")
_DEFAULTS: dict[str, CredentialStoreKind] = {"win32": "windows", "darwin": "keyring"}


class CredentialStoreSelectionError(ValueError):
    """The requested credential store cannot be used on this platform."""


@dataclass(frozen=True, slots=True)
class SelectedCredentialStore:
    store: CredentialStorePort
    kind: CredentialStoreKind
    detail: str
    weaker: bool


def choose_credential_store_kind(
    platform: str | None = None, environ: Mapping[str, str] | None = None
) -> CredentialStoreKind:
    resolved_platform = sys.platform if platform is None else platform
    source = os.environ if environ is None else environ
    requested = source.get(CREDENTIAL_STORE_ENV, "").strip().lower()
    if requested:
        if requested not in _KINDS:
            raise CredentialStoreSelectionError(
                f"{CREDENTIAL_STORE_ENV}={requested!r} is not one of {', '.join(_KINDS)}."
            )
        kind: CredentialStoreKind = requested  # type: ignore[assignment]
    else:
        kind = _DEFAULTS.get(resolved_platform, "file")
    if kind == "windows" and resolved_platform != "win32":
        raise CredentialStoreSelectionError(
            "The Windows Credential Manager store is only available on Windows."
        )
    if kind == "file" and resolved_platform == "win32":
        raise CredentialStoreSelectionError(
            "The file credential store relies on POSIX permissions and is not "
            "available on Windows; use the default Credential Manager store."
        )
    return kind


def build_credential_store(kind: CredentialStoreKind, store_directory: Path) -> SelectedCredentialStore:
    if kind == "windows":
        from backend.app.credentials.windows_store import WindowsCredentialStore

        return SelectedCredentialStore(WindowsCredentialStore(), kind,
                                       "Windows Credential Manager", weaker=False)
    if kind == "keyring":
        from backend.app.credentials.keyring_store import KeyringCredentialStore

        store = KeyringCredentialStore()
        return SelectedCredentialStore(store, kind, f"OS keyring ({store.backend_name})",
                                       weaker=False)
    from backend.app.credentials.file_store import FileCredentialStore

    file_store = FileCredentialStore(store_directory)
    return SelectedCredentialStore(file_store, kind, f"owner-only file {file_store.path}",
                                   weaker=True)


def select_credential_store(
    store_directory: Path,
    *,
    logger: logging.Logger,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> SelectedCredentialStore:
    """Choose, build and announce the credential store (one startup log line)."""
    selected = build_credential_store(choose_credential_store_kind(platform, environ),
                                      store_directory)
    if selected.weaker:
        logger.warning(
            "Credential store: %s (%s). Secrets are readable by any process running as "
            "this user; set %s=keyring to use the OS keyring instead.",
            selected.kind, selected.detail, CREDENTIAL_STORE_ENV,
        )
    else:
        logger.info("Credential store: %s (%s)", selected.kind, selected.detail)
    return selected
