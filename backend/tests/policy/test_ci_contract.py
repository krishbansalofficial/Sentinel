"""The release workflow runs the default Python suite on every push, on Windows and Linux."""

from __future__ import annotations

from pathlib import Path


def _workflow() -> str:
    return (Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8")


def test_windows_ci_runs_default_suite_on_every_push() -> None:
    text = _workflow()
    assert "  push:" in text
    assert "runs-on: windows-latest" in text
    assert "python-version: ['3.12', '3.14']" in text
    assert "python-version: ${{ matrix.python-version }}" in text
    assert 'python -m pip install -e ".[test,tui,keyring]"' in text
    assert "run: python -m pytest -q" in text


def test_linux_sandbox_ci_requires_the_real_boundary_tests() -> None:
    text = _workflow()
    job = text[text.index("linux-sandbox:"):]
    assert "apt-get install -y -q bubblewrap" in job
    assert "SENTINEL_REQUIRE_LINUX_SANDBOX: '1'" in job
    assert "backend/tests/execution/linux" in job


def test_linux_ci_collects_and_runs_the_default_suite() -> None:
    text = _workflow()
    linux = text[text.index("pytest-linux:"):]
    assert "runs-on: ubuntu-latest" in linux
    assert "python-version: ['3.12', '3.14']" in linux
    assert "run: python -m pytest --collect-only -q" in linux
    assert "run: python -m pytest -q" in linux
