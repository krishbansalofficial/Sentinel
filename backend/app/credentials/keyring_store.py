"""A `CredentialStorePort` backed by the OS keyring through `keyring`.

macOS: the login Keychain. Linux: the Secret Service (GNOME Keyring, KWallet).
`keyring` is a dependency on macOS and an optional extra elsewhere
(``pip install "change-assurance[keyring]"``).

Construction fails closed: if `keyring` is not installed, or its active backend
is one that does not actually keep secrets (``fail.Keyring``, ``null.Keyring``,
or the chainer with nothing behind it), this raises instead of returning a
store that silently drops or refuses every secret.
"""

from __future__ import annotations

from typing import Any

SERVICE_NAME = "ChangeAssuranceRuntime"
_NON_STORING_BACKENDS = {
    "keyring.backends.fail.Keyring",
    "keyring.backends.null.Keyring",
}


class KeyringUnavailableError(RuntimeError):
    """No usable OS keyring backend is available."""


def _backend_name(backend: object) -> str:
    kind = type(backend)
    return f"{kind.__module__}.{kind.__qualname__}"


def usable_backend(keyring_module: Any) -> object:
    """Return the active backend, or raise when it cannot store secrets."""
    backend = keyring_module.get_keyring()
    name = _backend_name(backend)
    if name in _NON_STORING_BACKENDS:
        raise KeyringUnavailableError(f"The active keyring backend {name} does not store secrets.")
    if name == "keyring.backends.chainer.ChainerBackend" and not getattr(backend, "backends", None):
        raise KeyringUnavailableError("No keyring backend is available behind the chainer.")
    return backend


class KeyringCredentialStore:
    """Concrete `CredentialStorePort` backed by `keyring`."""

    def __init__(self, service: str = SERVICE_NAME, *, keyring_module: Any = None) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module  # type: ignore[no-redef]
            except ImportError as exc:
                raise KeyringUnavailableError(
                    "The 'keyring' package is not installed. Install it with "
                    "`pip install keyring`, or set SENTINEL_CREDENTIAL_STORE=file "
                    "to use the weaker owner-only file store."
                ) from exc
        self._keyring = keyring_module
        self._backend = usable_backend(keyring_module)
        self._service = service

    @property
    def backend_name(self) -> str:
        return _backend_name(self._backend)

    def put(self, key: str, secret: str) -> None:
        self._keyring.set_password(self._service, key, secret)

    def get(self, key: str) -> str | None:
        return self._keyring.get_password(self._service, key)

    def delete(self, key: str) -> bool:
        errors = getattr(self._keyring, "errors", None)
        delete_error = getattr(errors, "PasswordDeleteError", None)
        if self._keyring.get_password(self._service, key) is None:
            return False
        try:
            self._keyring.delete_password(self._service, key)
        except Exception as exc:  # keyring raises PasswordDeleteError for a missing entry
            if delete_error is not None and isinstance(exc, delete_error):
                return False
            raise
        return True
