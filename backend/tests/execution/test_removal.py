"""The shared removal retry: transient Windows errors retry with growing delay, others raise."""

from __future__ import annotations

import pytest

from backend.app.execution._removal import is_transient, remove_with_retries


def _winerror(code: int) -> OSError:
    error = OSError(f"winerror {code}")
    error.winerror = code
    return error


class Flaky:
    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


def test_transient_errors_retry_with_a_growing_delay_then_succeed() -> None:
    sleeps: list[float] = []
    remove = Flaky(_winerror(32), PermissionError("in use"), _winerror(5))
    remove_with_retries(remove, attempts=5, backoff_seconds=0.25, sleep=sleeps.append)
    assert remove.calls == 4 and sleeps == [0.25, 0.5, 0.75]


def test_the_last_transient_error_is_raised_unchanged() -> None:
    last = _winerror(32)
    remove = Flaky(_winerror(32), _winerror(32), last)
    with pytest.raises(OSError) as caught:
        remove_with_retries(remove, attempts=3, backoff_seconds=0, sleep=lambda _s: None)
    assert caught.value is last and remove.calls == 3


@pytest.mark.parametrize("error", [FileNotFoundError("gone"), _winerror(2), _winerror(145), OSError("x")])
def test_non_transient_errors_raise_immediately(error) -> None:
    remove = Flaky(error)
    sleeps: list[float] = []
    with pytest.raises(OSError) as caught:
        remove_with_retries(remove, attempts=6, backoff_seconds=1, sleep=sleeps.append)
    assert caught.value is error and remove.calls == 1 and sleeps == []


def test_non_os_errors_are_never_swallowed() -> None:
    remove = Flaky(RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        remove_with_retries(remove, attempts=3, backoff_seconds=0, sleep=lambda _s: None)
    assert remove.calls == 1


def test_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError):
        remove_with_retries(lambda: None, attempts=0, backoff_seconds=0)


def test_transient_classification() -> None:
    assert is_transient(PermissionError("denied"))
    assert is_transient(_winerror(5)) and is_transient(_winerror(32))
    assert not is_transient(_winerror(2)) and not is_transient(FileNotFoundError("x"))
