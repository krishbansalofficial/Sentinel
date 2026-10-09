# Adversarial sandbox fuzzing (Linux)

## Windows run (2026-10-04)

`backend/tests/execution/test_fuzz_appcontainer.py` runs seeded combinations of outside writes,
contract writes, mock-secret reads (raw/base64), junction traversal, local API access, detached
spawn attempts, and rapid spawn/exit attempts. It uses a real AppContainer without network
capabilities and checks host files, listener connections and reported PID cleanup from outside.
Each scenario must also write a positive-control canary inside its own container.

Hardware: Windows, Intel Core i9-14900HX, Python 3.14.3, Node 24.14.0 under Program Files.

```powershell
$env:SENTINEL_FUZZ_SCENARIOS='12'
python -m pytest backend/tests/execution/test_fuzz_appcontainer.py -q -p no:cacheprovider --junitxml=bench/fuzz/windows-results.xml
```

| Seed | Scenarios | Behavior attempts | Escapes observed | Escapes fixed |
| --- | --- | --- | --- | --- |
| 20261004 | 12 | 48 | 0 | 0 |

Raw output: `windows-results.xml` (12 tests, zero failures). Node child creation was refused
on this host; detached/spawn-burst counts are attempts, not successfully created descendants.
The first harness used default child pipes and hung, then incorrectly required child creation
to succeed. Hidden windows, ignored child stdio and explicit denial reporting corrected the
harness. No security escape was found. Windows has no fork/setsid; Linux covers those behaviors.
This run does not claim exhaustive coverage of junction races or every credential encoding.

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
