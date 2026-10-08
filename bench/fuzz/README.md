# Adversarial sandbox fuzzing (Linux)

`backend/tests/execution/linux/test_fuzz_sandbox.py` generates seeded hostile scenarios, each
combining 3 to 5 behaviors: writes outside the workspace (direct and through a planted
symlink), reads of a mock secret (raw and base64), connections to a live host listener
standing in for the local API and to the internet, double-fork/setsid daemons, rapid spawn
bursts, and unshare/ptrace/mount. The host checks every invariant from outside, with positive
controls (the host can reach the listener and read the secret).

## Result (2026-10-08)

| Seed | Scenarios | Behaviors run | Escapes found | Escapes fixed |
| --- | --- | --- | --- | --- |
| 20261008 | 40 | 153 | 0 | 0 |

```bash
SENTINEL_FUZZ_SCENARIOS=40 SENTINEL_FUZZ_REPORT=/tmp/fuzz.json \
  python -m pytest -q backend/tests/execution/linux/test_fuzz_sandbox.py
```

Privileged Docker container on Windows 11 + WSL2 (cgroup2 without controllers), bubblewrap
0.8.0, Python 3.12.15; the CI `linux-sandbox` job runs 25 scenarios on every push. Not yet
covered: the same fuzzer against the Windows AppContainer launch.
