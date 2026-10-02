"""The verifier's usage-error handling works with and without Typer's private vendored Click."""

from __future__ import annotations

import importlib
import sys
import types

from click.exceptions import UsageError as ClickUsageError

import backend.app.cli.passport_commands as passport_commands


def _reload_with(monkeypatch, module: types.ModuleType | None):
    monkeypatch.setitem(sys.modules, "typer._click", module and types.ModuleType("typer._click"))
    monkeypatch.setitem(sys.modules, "typer._click.exceptions", module)
    return importlib.reload(passport_commands)


def test_vendored_click_usage_error_is_used_when_present(monkeypatch) -> None:
    vendored = types.ModuleType("typer._click.exceptions")

    class VendoredUsageError(Exception):
        pass

    vendored.UsageError = VendoredUsageError
    try:
        reloaded = _reload_with(monkeypatch, vendored)
        assert reloaded.UsageError is VendoredUsageError
        assert reloaded.ClickUsageError is ClickUsageError
    finally:
        monkeypatch.undo()
        importlib.reload(passport_commands)


def test_click_usage_error_is_the_fallback_without_vendored_click(monkeypatch) -> None:
    try:
        reloaded = _reload_with(monkeypatch, None)  # None in sys.modules -> ImportError
        assert reloaded.UsageError is ClickUsageError
    finally:
        monkeypatch.undo()
        importlib.reload(passport_commands)
