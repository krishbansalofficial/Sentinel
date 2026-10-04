"""The keyring-backed credential store, against a fake `keyring` module."""

from __future__ import annotations

import sys
import types

import pytest

from backend.app.credentials.keyring_store import (
    SERVICE_NAME,
    KeyringCredentialStore,
    KeyringUnavailableError,
)


class _PasswordDeleteError(Exception):
    pass


def _backend_class(module: str, name: str) -> type:
    return type(name, (), {"__module__": module})


def fake_keyring(backend: object | None = None) -> types.SimpleNamespace:
    """A minimal stand-in for the `keyring` API with an in-memory vault."""
    vault: dict[tuple[str, str], str] = {}
    chosen = backend or _backend_class("keyring.backends.SecretService", "Keyring")()

    def delete_password(service: str, key: str) -> None:
        if (service, key) not in vault:
            raise _PasswordDeleteError(key)
        del vault[(service, key)]

    return types.SimpleNamespace(
        vault=vault,
        get_keyring=lambda: chosen,
        set_password=lambda service, key, secret: vault.__setitem__((service, key), secret),
        get_password=lambda service, key: vault.get((service, key)),
        delete_password=delete_password,
        errors=types.SimpleNamespace(PasswordDeleteError=_PasswordDeleteError),
    )


def test_round_trip_uses_the_service_namespace() -> None:
    module = fake_keyring()
    store = KeyringCredentialStore(keyring_module=module)
    assert store.backend_name == "keyring.backends.SecretService.Keyring"
    assert store.get("provider:github") is None
    store.put("provider:github", "ghp_mock_abc")
    assert module.vault == {(SERVICE_NAME, "provider:github"): "ghp_mock_abc"}
    assert store.get("provider:github") == "ghp_mock_abc"
    assert store.delete("provider:github") is True
    assert store.delete("provider:github") is False
    assert module.vault == {}


def test_delete_race_reports_missing() -> None:
    module = fake_keyring()
    store = KeyringCredentialStore(keyring_module=module)
    store.put("k", "v")
    real_get = module.get_password
    # Another process deletes between our check and our delete.
    module.get_password = lambda service, key: (module.vault.clear(), "v")[1]
    assert store.delete("k") is False
    module.get_password = real_get


def test_unrelated_delete_errors_propagate() -> None:
    module = fake_keyring()
    store = KeyringCredentialStore(keyring_module=module)
    store.put("k", "v")

    def broken(service: str, key: str) -> None:
        raise RuntimeError("D-Bus went away")

    module.delete_password = broken
    with pytest.raises(RuntimeError, match="D-Bus"):
        store.delete("k")


@pytest.mark.parametrize("module_name", ["keyring.backends.fail", "keyring.backends.null"])
def test_non_storing_backends_fail_closed(module_name: str) -> None:
    backend = _backend_class(module_name, "Keyring")()
    with pytest.raises(KeyringUnavailableError, match="does not store secrets"):
        KeyringCredentialStore(keyring_module=fake_keyring(backend))


def test_empty_chainer_fails_closed() -> None:
    chainer = _backend_class("keyring.backends.chainer", "ChainerBackend")()
    chainer.backends = []
    with pytest.raises(KeyringUnavailableError, match="chainer"):
        KeyringCredentialStore(keyring_module=fake_keyring(chainer))
    chainer.backends = [object()]
    assert KeyringCredentialStore(keyring_module=fake_keyring(chainer)).get("x") is None


def test_missing_package_explains_the_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # makes `import keyring` raise
    with pytest.raises(KeyringUnavailableError) as raised:
        KeyringCredentialStore()
    assert "pip install keyring" in str(raised.value)
    assert "SENTINEL_CREDENTIAL_STORE=file" in str(raised.value)
