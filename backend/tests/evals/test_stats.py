"""Eval statistics against published values and constructed cases."""

from __future__ import annotations

import pytest

from backend.app.evals.stats import (
    Interval,
    is_regression,
    paired_bootstrap,
    percentile,
    wilson_interval,
)


# Published Wilson 95% intervals (Newcombe 1998, Table I, and the 8/10 textbook case).
@pytest.mark.parametrize(("s", "n", "low", "high"), [
    (81, 263, 0.2553, 0.3662),
    (15, 148, 0.0624, 0.1605),
    (0, 20, 0.0000, 0.1611),
    (1, 29, 0.0061, 0.1718),
    (8, 10, 0.4902, 0.9433),
    (0, 10, 0.0000, 0.2775),
    (10, 10, 0.7225, 1.0000),
])
def test_wilson_matches_published_values(s, n, low, high) -> None:
    interval = wilson_interval(s, n)
    assert interval.low == pytest.approx(low, abs=1e-4)
    assert interval.high == pytest.approx(high, abs=1e-4)


def test_wilson_edges_and_refusals() -> None:
    assert wilson_interval(0, 0) == Interval(0.0, 1.0)
    for s, n in ((0, 3), (3, 3), (1, 3)):
        interval = wilson_interval(s, n)
        assert 0.0 <= interval.low <= s / n <= interval.high <= 1.0
    for bad in ((-1, 3), (4, 3), (0, -1)):
        with pytest.raises(ValueError):
            wilson_interval(*bad)


def test_wilson_narrows_with_more_attempts() -> None:
    widths = [wilson_interval(n // 2, n).high - wilson_interval(n // 2, n).low
              for n in (4, 16, 64, 256)]
    assert widths == sorted(widths, reverse=True)


def test_percentile_interpolates() -> None:
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([5], 0.95) == 5
    assert percentile([10, 20, 30], 0.0) == 10 and percentile([10, 20, 30], 1.0) == 30
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_identical_runs_have_a_zero_bootstrap_interval() -> None:
    rates = {f"t{i}": (i % 3) / 2 for i in range(30)}
    result = paired_bootstrap(rates, dict(rates))
    assert result.mean_difference == 0.0
    assert result.interval == Interval(0.0, 0.0)
    assert not result.significant_drop and not result.significant_gain


def test_a_uniform_collapse_is_a_significant_drop() -> None:
    a = {f"t{i}": 1.0 for i in range(20)}
    b = {f"t{i}": 0.0 for i in range(20)}
    result = paired_bootstrap(a, b)
    assert result.mean_difference == -1.0 and result.interval == Interval(-1.0, -1.0)
    assert result.significant_drop


def test_bootstrap_is_reproducible_with_a_seed_and_uses_shared_tasks_only() -> None:
    a = {"x": 1.0, "y": 0.5, "z": 0.0, "only_a": 1.0}
    b = {"x": 0.5, "y": 0.5, "z": 0.5, "only_b": 0.0}
    first = paired_bootstrap(a, b, resamples=2000, seed=7)
    assert first == paired_bootstrap(a, b, resamples=2000, seed=7)
    assert first.tasks == 3
    with pytest.raises(ValueError):
        paired_bootstrap({"a": 1.0}, {"b": 1.0})


def test_regression_needs_separated_intervals_or_a_significant_bootstrap() -> None:
    noisy = paired_bootstrap({"a": 1.0, "b": 0.0}, {"a": 0.0, "b": 1.0})
    assert not is_regression((27, 30), (25, 30), noisy)  # overlapping, no significant drop
    assert is_regression((30, 30), (5, 30), None)  # intervals separate
    drop = paired_bootstrap({f"t{i}": 1.0 for i in range(10)}, {f"t{i}": 0.0 for i in range(10)})
    assert is_regression((10, 10), (8, 10), drop)
