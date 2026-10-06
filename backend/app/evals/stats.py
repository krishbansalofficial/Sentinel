"""Statistics for agent evals: Wilson intervals, a paired bootstrap, percentiles.

Agents are nondeterministic, so every task runs ``k`` times and a pass rate is
reported with its uncertainty instead of as a bare number:

* :func:`wilson_interval` gives the 95% Wilson score interval for ``s`` passes in
  ``n`` attempts. Unlike the normal approximation it stays inside [0, 1] and is
  sensible at 0/n and n/n, which small k makes common.
* :func:`paired_bootstrap` compares two runs over the *same* tasks. It resamples
  tasks with replacement (keeping each task's pair together, so task difficulty
  cancels) and returns the mean per-task pass-rate difference with a percentile
  95% interval. A fixed seed makes it reproducible.
* :func:`is_regression` flags a drop only when the two Wilson intervals do not
  overlap, or the bootstrap interval of the difference lies entirely below zero.
  Two runs of the same configuration therefore do not flag on noise.

Pure module: no I/O, no randomness without a seed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

Z_95 = 1.959963984540054  # two-sided 95% normal quantile
DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 20261003


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float

    def separated_below(self, other: Interval) -> bool:
        """True when this interval lies entirely below ``other``."""
        return self.high < other.low


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> Interval:
    """The Wilson score interval for ``successes`` out of ``trials`` (0/0 is [0, 1])."""
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("need 0 <= successes <= trials")
    if trials == 0:
        return Interval(0.0, 1.0)
    p = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    center = (p + z2 / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denominator
    # Clamp the floating error at the edges (0/n and n/n are exact 0 and 1).
    low = 0.0 if successes == 0 else max(0.0, center - half)
    high = 1.0 if successes == trials else min(1.0, center + half)
    return Interval(low, high)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile (``fraction`` in [0, 1]) of a non-empty sequence."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be in [0, 1]")
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    mean_difference: float  # mean over tasks of (rate_b - rate_a)
    interval: Interval
    resamples: int
    tasks: int

    @property
    def significant_drop(self) -> bool:
        return self.interval.high < 0.0

    @property
    def significant_gain(self) -> bool:
        return self.interval.low > 0.0


def paired_bootstrap(rates_a: Mapping[str, float], rates_b: Mapping[str, float], *,
                     resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED,
                     confidence: float = 0.95) -> BootstrapResult:
    """Bootstrap the mean per-task difference over the tasks both runs share."""
    tasks = sorted(set(rates_a) & set(rates_b))
    if not tasks:
        raise ValueError("the runs share no tasks")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    differences = [rates_b[task] - rates_a[task] for task in tasks]
    observed = sum(differences) / len(differences)
    generator = random.Random(seed)
    count = len(differences)
    means = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(count):
            total += differences[generator.randrange(count)]
        means.append(total / count)
    tail = (1.0 - confidence) / 2.0
    return BootstrapResult(observed, Interval(percentile(means, tail), percentile(means, 1 - tail)),
                           resamples, count)


def is_regression(a: tuple[int, int], b: tuple[int, int], bootstrap: BootstrapResult | None) -> bool:
    """``a``/``b`` are (passes, attempts). A drop counts only when it is not noise."""
    interval_a = wilson_interval(*a)
    interval_b = wilson_interval(*b)
    if interval_b.separated_below(interval_a):
        return True
    return bootstrap is not None and bootstrap.significant_drop
