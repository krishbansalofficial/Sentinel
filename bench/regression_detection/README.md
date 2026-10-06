# Regression detection backtest (mock agent)

Does `sentinel eval compare` flag injected regressions, and leave same-config runs alone?
Everything here uses the **mock agent**: it applies each task's reference solution with a seeded
probability (`skill`), so the true pass rate is known. Cost and tokens are synthetic. This
measures the eval pipeline and the statistics. It is not a model benchmark.

## Part A: real pipeline, 30-task seed suite, k = 3 (2026-10-06)

Real fixture repositories, real hidden tests, all **90/90 attempts per config confined in the
verified Linux sandbox** (`hidden_boundaries = {"LINUX_SANDBOX": 90}`), 0 errors.

| Config | Injected change | Passed | Rate | Wilson 95% |
| --- | --- | ---: | ---: | --- |
| baseline-a | skill 0.85, seed 1 | 77/90 | 85.6% | 76.8%–91.4% |
| baseline-b | skill 0.85, seed 2 | 75/90 | 83.3% | 74.3%–89.6% |
| degraded | skill 0.50 (model regression) | 47/90 | 52.2% | 42.0%–62.2% |
| broken-prompt | template `{title}` drops the task prompt | 0/90 | 0.0% | 0.0%–4.1% |

| Compare | Expected | Flagged | Mean delta (paired bootstrap 95%) |
| --- | --- | --- | --- |
| baseline-a → baseline-b | no | no | −0.022 (−0.144 to +0.089) |
| baseline-a → degraded | yes | **yes** | −0.333 (−0.456 to −0.211) |
| baseline-a → broken-prompt | yes | **yes** | −0.856 (−0.922 to −0.789) |

## Part B: Monte Carlo, 200 trials per row

Synthetic result documents (30 tasks, k = 3, per-attempt Bernoulli outcomes at the baseline
rate 0.85 and at 0.85 − drop), compared with `report.compare` exactly as the CLI does
(bootstrap resamples 2,000).

| True drop | Flagged | Rate |
| ---: | ---: | ---: |
| 0.00 (false positives) | 7/200 | **3.5%** |
| 0.05 | 33/200 | 16.5% |
| 0.10 | 77/200 | 38.5% |
| 0.15 | 136/200 | 68.0% |
| 0.20 | 175/200 | 87.5% |
| 0.30 | 198/200 | 99.0% |

The false-positive rate (3.5%, Wilson 95% roughly 1.7%–7.0%) matches the design: a drop
counts when the two-sided 95% bootstrap interval lies wholly below zero (about 2.5% by
construction) or the Wilson intervals separate. With 90 attempts per run, drops of 0.20 or more
are caught reliably; drops of 0.10 or less mostly are not. Use a larger k or more tasks to
resolve smaller regressions.

## Exact command and environment

```bash
# Linux sandbox prerequisites: bubblewrap, user namespaces, and a cgroup v2 subtree in
# SENTINEL_CGROUP_PARENT. --no-controllers accepts one without pids/memory (this host).
python bench/regression_detection/run.py --confined --no-controllers \
  --out bench/regression_detection/results --trials 200
```

Linux 6.18 cloud container, Python 3.13.16, bubblewrap 0.9.0, cgroup2 bound over
`/sys/fs/cgroup` inside a private mount namespace (the host has a hybrid v1/v2 layout, so the
pids/memory limits were not applied; membership, freeze and kill were). Total 96 s. Raw output:
[`results/summary.json`](results/summary.json) and the four per-config result files and HTML
reports in `results/`. Without `--confined` the hidden tests run as host children and every
result says `UNCONFINED`.
