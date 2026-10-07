# Sentinel: Handoff for the Next Build Phase

Owner: Krish Bansal (kb). Fork: `krishbansalofficial/Sentinel` (upstream `csshlok/Sentinel`).
Written: 2026-10-03. Historical status updated 2026-10-09 (the prior handoff date is retained as recorded). Read this whole file before touching code.

## Fork maintenance authority (2026-10-06)

Krish Bansal maintains this fork independently after the original hackathon.
Implementation, fixes, tests and local builds are authorized by the fork owner;
no approval from csshlok or the former team is required. Earlier team approval
requirements below are historical and superseded. Existing upstream authorship
and third-party license obligations still apply; this statement does not assign
copyright or relicense upstream code.

## Current fork continuation (2026-10-06)

Read [HANDOFF_FORK.md](HANDOFF_FORK.md) for this session's fix log, reproducible
commands, evidence and next-agent cautions.

The table and dated test counts below describe the inherited hackathon work.
This continuation implemented the cargo/.NET confinement handoff, repaired
hidden-test verification and isolation, updated vulnerable dependencies, and
hardened the Windows package staging. The frontend, backend, real browser flows
and self-contained Electron application are implemented and exercised locally.
See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for the security findings and limits.

Current desktop validation: 69 renderer tests, 108 Electron tests and 113 browser
tests passed; production build, TypeScript and API consistency checks passed.
Browser validation combined 112 passing tests from the full run with a passing
rerun of the dialog stress test after giving its 80 animated dismissals a
60-second deadline. Its trace showed continued progress rather than a stuck dialog.
The packaged Electron application passed all 14 smoke checks, including real
pytest and diff-coverage checks with its bundled Python 3.14.8 and pytest 9.0.3 under APPCONTAINER. Real captures and
an animated screen tour are in `bench/release/walkthrough/` and
`bench/release/walkthrough.gif`.

The real Rust and SDK-only .NET AppContainer probes passed, including host
positive controls for denied file and network access. Full backend suite results
are recorded at the end of this file when complete. The final dependency reports
are `bench/release/npm-audit.json` and `bench/release/python-audit.json`.

A real authenticated Claude attempt launched inside verified AppContainer;
the provider refused it for weekly quota exhaustion. Its ERROR result is saved
in `bench/release/claude-local.json` and `.html`. No model accuracy is claimed.
This WSL host uses hybrid cgroups. Real Linux isolation tests run in a private
mount namespace over its existing cgroup v2 mount, with unavailable resource
controllers explicitly disabled only in the test harness. Memory and process-count
enforcement still need the existing dedicated CI job or a suitable host. New remote
CI execution, package registration/publication, Pages hosting and launch posts
remain external release steps. No upstream/team implementation approval applies.

## 0. Status at a glance (read first)

| Phase | State | Where |
| --- | --- | --- |
| 0. Boot and test on Linux/macOS | **Done**, pushed | commits `cd1bf89`..`8236ca1` |
| 1. Verified Linux agent boundary | **Done**, including Linux confined check boxes; Linux CI confirmed, Windows CI test fixes await a new run | `0811441`..`c70b9e5` |
| 2. `sentinel eval` | **Done**: stats, suite, runner, drivers, CLI (`run/compare/report`, `--record`), HTML report, 30-task seed suite, backend store + API + journal events | `5e439dc`..`f10f8e2` |
| 3. Queue and crash recovery | **Done**: queue, pool, fencing, SIGKILL + chaos tests, bench, `eval run --workers/--queue/--resume` | `997406e`, `5ad8598` |
| 4. Observability | **Done**: optional OTLP spans (`[telemetry]` extra), connected Jaeger trace, `/api/v1/metrics` (hardened: counts only Sentinel's own refusal lines), desktop Eval page with run comparison | `75962e0`, `b00ad7b`, `a934b9f`, `4c6a6e4` |
| 5. Adversarial fuzzer | **Linux done** (40 scenarios, 153 behaviors, 0 escapes); Windows seeded AppContainer fuzzer passes 12 scenarios / 48 behavior attempts, 0 observed escapes | `bench/fuzz/`, `9cc2d40`, `42aea0a`; Windows child-spawn attempts were refused on this host |
| 6. Release and users | **Mostly done**: Apache-2.0 license, `sentinel-runtime` package with a tag-triggered PyPI release workflow (trusted publishing), doctor, contribution docs/templates, README quickstart + honest mock eval table, leaderboard Pages workflow. Remaining: README GIF; (need kb) PyPI trusted-publisher setup and first `v0.1.0` tag, enabling Pages, real Claude results, launch | `bench/release/`, `.github/workflows/{release,leaderboard}.yml` |

Verification for `b00ad7b`..`42aea0a` (2026-10-09): Windows targeted suites (core, evals, policy, launcher, contract, AppContainer fuzz) green after the fixes in those commits; Linux full suite 2024 passed with the only failures being the README count and CI pin tests fixed in `42aea0a`/`b00ad7b` and telemetry tests needing the `[telemetry]` extra (86/86 green with it); desktop typecheck, api:check and 65 unit tests green. Docker is only the Windows dev box's way to reach a Linux kernel for tests; Sentinel never uses Docker at runtime.

### License, package and release (2026-10-06)
Decision (kb, 2026-10-06): no third-party permission is needed (the hackathon project is
continued independently). The project is **Apache-2.0** (`LICENSE`, `NOTICE`,
`pyproject.toml` `license`/`license-files`, README and CONTRIBUTING sections).
* Package: the distribution is now **`sentinel-runtime`** (free on PyPI when checked
  2026-10-06). Only the distribution name changed: the store directory, database filename,
  environment fingerprint key, backend service name and recovery branch prefix keep their
  `change-assurance`/`change_assurance` names, so existing stores and Passports still work.
  The wheel excludes `backend/tests` (524 KB). `twine check --strict` passes; a clean install
  from the wheel runs `sentinel --version`, `sentinel doctor` and the mock eval smoke.
* Release: `.github/workflows/release.yml` builds, checks, smoke-tests and publishes on a
  `v*` tag via PyPI trusted publishing (no token). To ship: on pypi.org add a pending trusted
  publisher (project `sentinel-runtime`, owner `krishbansalofficial`, repo `Sentinel`,
  workflow `release.yml`, environment `pypi`), then push tag `v0.1.0`.
* Existing local checkouts: after pulling, run `pip install -e ".[test,tui,keyring,telemetry]"`
  again so the new distribution name is registered (uninstall `change-assurance` first, then
  reinstall, because both own the `sentinel` script).

### Eval comparison everywhere, TUI Eval screen, leaderboard publication (2026-10-06)
* API (additive): `GET /api/v1/evals/runs/{id}/attempts` (per-attempt results, unknown
  fields null) and `GET /api/v1/evals/compare?baseline=&candidate=` (the CLI's comparison:
  per-task deltas, flips, paired bootstrap, regression verdict) over stored attempts.
  `openapi.json` and the desktop client regenerated.
* Desktop Eval page: the compare panel shows the backend's bootstrap verdict (no longer
  "use the CLI"); each run has an on-demand Attempts table. Unit and Playwright tests added.
* TUI: `e` on the dashboard opens an Eval screen (runs, intervals, latest-vs-previous
  verdict with flipped tasks), tested against a live server.
* Phase 6.4: `.github/workflows/leaderboard.yml` publishes the static leaderboard and
  per-run reports from committed results to GitHub Pages; a test runs the same build.
  Enable Pages ("GitHub Actions" source) in the repository settings before the first run.
  The Pages actions are tag-pinned (`@v3`/`@v4`); pin them to SHAs like `ci.yml` if wanted.
* Still open (needs a person or a host): a real `--agent claude` run, the first PyPI
  release, Windows CI confirmation, Windows fuzz follow-ups and launch. `/metrics`
  has no queue depth because the eval queue lives beside the results file, not in the backend.

### Eval audit and mock backtest (2026-10-06)
Bugs found and fixed (each has a regression test that fails on the old code):
* `SandboxHiddenTestRunner` read the wrapper's `returncode`, which `communicate()` on the
  inner Popen never sets: every confined Linux hidden test scored FAILED (0% pass rate).
  Confirmed on a real kernel. `LinuxSandboxProcess.communicate_with_timeout` now owns the
  wait, timeout kill and return code; only `TimeoutExpired` counts as a timeout.
* `VerificationHiddenTestRunner` (Windows) posted a flat body to `/verify`, which takes
  `VerificationActionRequest` (an actor plus nested verification): every Windows confined
  hidden test would 422. It now creates an actor with a `change.legacy_verify` delegation
  and clamps the timeout to the contract's 300 s.
* The hidden-test cgroup hierarchy was lazily prepared without a lock (races with
  `--workers`); `spawn_linux_sandbox` leaked the run cgroup when bwrap was missing;
  `hidden_tests_absent` matched empty hidden files (`__init__.py`) against any empty fixture
  file; prompt templates substituted `{title}` inside the substituted prompt.
* Desktop Electron tests: 12 of 104 were cancelled on Node 22 (fakes held no handle while
  unref()ed timers were awaited); fixed in the tests. Now 104/104.
* Tests: a positive control depended on the runner's global `gpg.format`; seccomp
  "native architecture" skips were impossible parametrizations; stale skip reasons.
* Added Linux confined Go tests (`execution/linux/test_linux_confined_go.py`) and real-kernel
  hidden-runner tests (`execution/linux/test_hidden_sandbox.py`).
* Backtest: `bench/regression_detection/` (360 confined attempts, 3/3 correct verdicts;
  false-positive rate 3.5% over 200 same-config trials).

Verification (Linux 6.18 container, Python 3.13): full suite as root and as a non-root user
(see the commit for counts); real-kernel suite 205 passed, 2 skipped (pids/memory controllers
unavailable on this hybrid-cgroup host); desktop api:check, typecheck, 69 unit and 104
Electron tests. Remaining skips are Windows-only, macOS-only or the opt-in live Claude test.

### Linux confined check boxes: implemented
The Linux platform in `execution/linux_check_box.py` supplies the existing `BoxPlatform`
seam: private 0700 storage, Linux identities, scratch HOME/TMPDIR, and verified bubblewrap,
seccomp and cgroup execution. Runtime binds are read-only: Python's venv/base install,
Node's prefix and project node_modules, and offline GOROOT. Unsafe runtime roots and
Cargo and .NET SDKs now have read-only runtime binds and private offline caches; uv remains refused. Linux has no Windows runtime snapshot cache.

Check runs journal Linux facts under `LINUX_SANDBOX`. Row and journal verification must
agree with the box identity and network setting; AppContainer and Linux facts never
cross-accept. Missing or tampered evidence never yields PASS. The additive contract,
OpenAPI and desktop client include Linux facts and boundaries; Assurance and Passport
show check runs with each boundary's own name. The displayed Linux verification flag is
computed from the facts, rather than trusting the recorded flag.

Verification for this continuation (2026-10-04):
* Final real-kernel and Linux unit suites: **78 passed, 2 skipped**, using
  `python -m pytest -o addopts= -q -p no:cacheprovider -rs -W ignore
  backend/tests/execution/linux backend/tests/execution/test_linux_check_box.py`.
  This includes **24 passed, 2 skipped** in the real-kernel folder and **54 passed**
  in the unit file. The HTTP tests carry `real_check_boxes` to disable the host fake.
  Harness: privileged `python:3.12-slim`, bubblewrap/git/nodejs/npm/procps, package
  installed with `pip install -e ".[test,tui,keyring,telemetry]"`, cgroup2 mounted,
  shell in a leaf, delegated subtree owned by tester, tests run as tester with
  `SENTINEL_TEST_CGROUP_PARENT=/sys/fs/cgroup/sentinel-test` and
  `SENTINEL_TEST_CGROUP_CONTROLLERS=0`. Fork-bomb/controller tests skip on this
  Docker Desktop kernel; the CI linux-sandbox job must verify them with controllers.
* Full Linux plain suite: **2087 passed, 328 skipped, 21 warnings** in 469.30 s.
  Command: `python -m pytest -o addopts= -q` in nonprivileged `python:3.12-slim`,
  git installed, package installed with `pip install -e ".[test,tui,keyring,telemetry]"`,
  and a test git identity configured; tracked and untracked working files streamed via tar.
* Full Windows suite: **2327 passed, 88 skipped, 3 warnings** in 2025.15 s.
  Command: `.tmp/windows-suite-env/Scripts/python.exe -m pytest -o addopts= -q
  --junitxml=windows-full-final.xml`, with a fresh Python 3.14.3 venv installed via
  `pip install -e ".[test,tui,keyring,telemetry]"`. No failures, including the
  dependency-pin and supervised-tree recovery tests. The earlier system-Python
  run was replaced after prolonged runtime-snapshot hashing; it is not counted
  as a completed run. System Python has FastAPI 0.135.1 versus the 0.141.1 pin.
* Final Windows affected suites: **96 passed, 3 POSIX skips**, using
  `python -m pytest -o addopts= -q backend/tests/execution/test_linux_check_box.py
  backend/tests/execution/test_check_box.py backend/tests/acceptance/test_contract_boundaries.py`.
* Desktop: `npm run api:check`, `npm test`, `npm run typecheck`, and `npm run build` passed;
  **69 unit tests and 104 Electron tests**, zero failures.
* CI run `37228136353` for `7a3d29e`: linux-sandbox and both full Linux jobs passed.
  Both Windows jobs failed eight seeded fuzzer scenarios because Node 22 throws
  synchronous `spawn EPERM`; Python 3.12 also hit the tools-table worker timing race.
  Local follow-up catches synchronous child-spawn refusals while preserving all
  completion/escape assertions, adds a real-AppContainer forced-denial regression,
  and waits for tools/approval results in the TUI test. Affected suites:
  **26 passed** with `.tmp/windows-suite-env/Scripts/python.exe -m pytest -o addopts= -q
  backend/tests/execution/test_fuzz_appcontainer.py
  backend/tests/tui/test_pilot_real_worker_flows.py`. Existing real AppContainer
  boundary suite: **20 passed** using the same command with
  `backend/tests/execution/test_appcontainer.py`. A new CI run must verify these fixes;
  passing local tests do not establish that a new Windows CI run will pass.

Decisions taken (kb, 2026-10-03): **D-03** built-in profiles declare a boundary per platform
(`boundaries=(("win32", APPCONTAINER), ("linux", LINUX_SANDBOX))`; an unnamed platform is
UNAVAILABLE). **LINUX_SANDBOX ranks with APPCONTAINER** (one strength class, own name, never
relabeled); the `strict` preset accepts either (preset version 1.4.0).

Verification as of the last push:
* Linux (Docker `python:3.12-slim`): full suite 1935 passed, 0 failed after the two pinned-module
  fixes in `2966ab7` (the run that found them reported 2 failures, both fixed and re-run).
  Python 3.14 with no git identity: passed except the dependency-pin test, fixed in `863bdf7`.
* Linux real-boundary suite (privileged container, cgroup2 without controllers): 16 passed,
  fork-bomb skipped locally (needs controllers; runs in CI's `linux-sandbox` job).
* Windows: affected areas re-run green (policy/passport/providers/launcher/profiles 492,
  workspace 167, desktop unit 61 + typecheck). A full Windows run after `5e439dc` had not
  finished when this was written; check CI.
* Known environment-only failure on this dev box: `test_git_recovery.py::
  test_real_recovery_execute_terminates_a_live_supervised_tree` counts 4 instead of 2 because the
  venv `python.exe` is a launcher stub. It fails identically on the pre-work commit `ce1948c`.
* CI: the first two Phase 0 pushes failed on the Linux job (a stale workflow pin test, then the
  Unix Node layout); both fixed (`8dbcafd`, `8236ca1`). Watch the runs after `5e439dc`, especially
  the new `linux-sandbox` job, which has not run on GitHub before.

### How to test Linux from this Windows machine
Docker Desktop's WSL2 kernel uses a hybrid cgroup v1 layout, so pids/memory/cpu controllers are
not available in containers (do not change `.wslconfig`: it restarts WSL and Docker).
* Plain suite: stream the tree into `python:3.12-slim` (tracked + untracked files via
  `git ls-files -co --exclude-standard | tar`), `pip install --no-deps -e .`, run pytest.
* Real sandbox: the same in a `--privileged` container with `bubblewrap` and official Node,
  `umount /sys/fs/cgroup; mount -t cgroup2 none /sys/fs/cgroup`, a subtree `chown`-ed to the test
  user with the shell moved into a leaf, then run as that user with
  `SENTINEL_TEST_CGROUP_PARENT=<subtree> SENTINEL_TEST_CGROUP_CONTROLLERS=0`.
* CI (`.github/workflows/ci.yml`, job `linux-sandbox`) does the real thing with sudo and
  `SENTINEL_REQUIRE_LINUX_SANDBOX=1` so a broken environment fails instead of skipping.

### What exists now (map for the next person)
* Credentials: `credentials/selection.py` (windows | keyring | file, `SENTINEL_CREDENTIAL_STORE`),
  `file_store.py` (0600 JSON, fail closed on modes), `keyring_store.py` (refuses fail/null backends).
* Store directory per platform: `core/evidence_store.default_store_directory`.
* Signing off Windows: `passport/file_key.py` (`SOFTWARE_FILE`), chosen in `passport/keys.py`.
* Capabilities: `core/capabilities.py` (`process_supervisor` UNSUPPORTED off Windows,
  `linux_sandbox` probed read-only, cached 60 s); `/health` has `platform` and
  `unsupported_capabilities`.
* Linux boundary: `execution/seccomp.py`, `execution/cgroups.py`, `execution/linux_sandbox.py`;
  launcher strategy `_launch_linux_sandbox`; workspace identity `workspace/profiles.py`
  (`ContainerProfiles`, `LinuxWorkspaceProfiles`); Passport `passport/boundary.py::_linux_launch`.
* Bench: `bench/boundary_overhead/` (setup 7.37 ms median, 8.15 ms p95; read the README).
* Evals: `backend/app/evals/{stats,suite,runner,agents,hidden}.py`, tests in `backend/tests/evals/`.

### Remaining work, in order
0. **Phase 4 follow-up**: confirm CI with the new `telemetry` optional extra; run a real
   authenticated Claude eval with tracing. Local acceptance includes a connected mock trace
   in Jaeger, desktop build/tests and three Eval browser tests. Spans cover launch, boundary
   verification, checks and eval jobs; W3C context crosses the CLI/API and worker threads.
1. **Phase 2 leftovers**: (done: tests for `SandboxHiddenTestRunner` and `VerificationHiddenTestRunner`);
   a first real `--agent claude` run (needs a Claude login) to fill the eval results table.
   Ubuntu 24.04 hosts need `packaging/apparmor/sentinel-bwrap` (read its trade-off note).
2. **Done: Linux confined check boxes** (Phase 1 item 6): verified Linux platform,
   read-only runtimes, journaled Linux facts, Passport confined_checks and desktop views;
   verification and controller limitations are recorded above.
3. **Phase 5/6 follow-up**: extend Windows scenarios to race junctions and exercise successfully
   spawned hostile descendants on a suitable host; review `bench/fuzz/windows-results.xml`.
   License (Apache-2.0), package name (`sentinel-runtime`) and release workflow are done;
   remaining: PyPI trusted-publisher setup and the first tag, enabling Pages, real agent
   results, a README GIF, then launch. No public release was made.

Implementation verification (2026-10-04): 87 existing eval/contract tests passed; 51 targeted
telemetry/hidden-runner/leaderboard/dependency/Windows boundary tests passed; 12 Windows fuzz
scenarios passed. Desktop unit/Electron suites, generated-client check, typecheck, production
build and three new Eval Playwright cases passed. Full Windows/Linux suites were not rerun.
Additional queue, eval API and CLI checks passed (115 cases after rerunning the one CLI
smoke failure). That failure also reproduced on an isolated HEAD snapshot: the temporary
uv-created environment lacked pip. `python -m ensurepip --upgrade` fixed the environment,
and the CLI smoke plus all five final tracing tests passed. No production workaround was added.
Windows hidden-test results no longer invent APPCONTAINER when boundary evidence is absent:
they report UNKNOWN. No API schema change was required.

## 1. The goal in one paragraph

Turn Sentinel from a Windows only hackathon runtime into an **open source, cross platform
runtime that supervises AI coding agents and measures them**: run agents inside a verified
boundary on Linux (and keep Windows working), score them against a task suite with hidden
tests, detect regressions when the model, prompt, or tools change, run many tasks
concurrently with crash recovery, and get real external users. Every phase must end with a
**number we measured**, because those numbers become resume bullets.

What we are NOT doing: adding Kafka, Kubernetes, Redis, or any infrastructure that a load test
has not proven we need. SQLite in WAL mode plus a worker pool is the default until data says
otherwise. Any new dependency needs a one line justification in the PR description.

## 2. How to work in this repo (rules for any human or AI agent)

These invariants already exist in the codebase. Breaking one is a bug, not a tradeoff.

1. **Fail closed.** If a boundary cannot be established and verified, the launch fails. Never
   fall back to a weaker boundary on exception. See the docstring at the top of
   `backend/app/execution/launcher.py` and `agent_profiles.py` (user decision D-01).
2. **Unknown is never PASS.** Missing or unverifiable evidence is reported as `UNKNOWN`.
   See `backend/app/passport/boundary.py`, where the Change claim is the *weakest* launch.
3. **The contract is frozen and versioned.** `openapi.json` at the repo root must match the
   live app (`backend/tests/acceptance/test_contract_boundaries.py`). If you change a route or
   model, regenerate it in the same commit:
   ```bash
   python -c "import json; from backend.app.main import create_app; json.dump(create_app().openapi(), open('openapi.json', 'w'), indent=2, sort_keys=True)"
   cd apps/desktop && npm run api:generate   # keeps the desktop client in sync
   ```
   Contract changes must be **additive**. Existing Passport v1 and v2 bundles must still verify.
4. **Migrations are additive only.** Add a new numbered migration in
   `backend/migrations/versions.py`; never edit an old one.
5. **Every mutation is journaled in the same transaction** (`backend/app/core/journal.py`).
   New state (eval runs, task results, queue entries) gets journal events too.
6. **Module dependency direction.** `execution/` must not import `workspace/` or
   `credentials/`; it talks to them through the Protocols in `execution/agent_ports.py`.
   Follow the same pattern for new modules: define a Protocol, implement it elsewhere, wire it
   in `backend/app/main.py` (the single composition root).
7. **Tests with every change.** Platform specific tests use `pytest.mark.skipif` with a clear
   reason (existing pattern: `skipif(not IS_WINDOWS, reason="AppContainers are Windows-only")`).
   Add a matching Linux marker for Linux only tests.
8. **Don't weaken Windows.** Windows CI (`.github/workflows/ci.yml`) must stay green.
9. **Honest numbers only.** Benchmarks go in `bench/` with the exact command, hardware, and raw
   output. No number goes in the README or a resume unless it can be reproduced from that folder.

Useful commands:

```bash
python -m pip install -e ".[test,tui,keyring,telemetry]"
python -m pytest -q                              # full suite (about 28 min on Windows)
python -m pytest backend/tests/execution -q      # one area
sentinel --version
cd apps/desktop && npm ci && npm test            # desktop unit tests
```

Note on docs: `.gitignore` ignores all `*.md` except the README and this file. Planning notes
under `.planning/` are local only by an earlier team decision.

## 3. Where things live (orientation)

| Area | Key files |
| --- | --- |
| Composition root | `backend/app/main.py` (`create_app`) |
| Frozen contract | `backend/app/contracts/models.py`, `contracts/ports.py`, `openapi.json` |
| Agent launch | `execution/launcher.py` (`AgentLauncher`, `_LaunchStrategy`, `_launch_appcontainer`, `_execute`) |
| Boundary profiles | `execution/agent_profiles.py` (`BoundaryKind`, `RuntimeProfile`, `BUILTIN_PROFILES`, `resolve_profile`) |
| Windows supervision | `execution/process_supervisor.py` (Job Objects), `execution/appcontainer.py` |
| Confined checks | `execution/check_box.py`, `execution/check_runtime.py` |
| Workspace clones | `workspace/manager.py` (`WorkspaceManager.ensure`, `finish_run`) |
| Credentials | `credentials/broker.py`, `credentials/windows_store.py` |
| Journal and replay | `core/journal.py`, `core/replay_service.py` |
| Boundary claim in Passport | `passport/boundary.py`, `passport/v2.py` |
| Policy presets | `policy/presets.py`, `policy/gate.py` |
| CLI | `cli/main.py` (`sentinel run ...`) |

Windows specific code (uses `ctypes.WinDLL` or Win32 APIs): `credentials/windows_store.py`,
`execution/acl.py`, `execution/appcontainer.py`, `execution/check_box.py`,
`execution/check_runtime.py`, `execution/process_supervisor.py`,
`execution/signal_control.py`, `passport/cng.py`.

## 4. Current state (verified 2026-10-03 on Linux, Python 3.13)

* `pip install -e ".[test,tui]"` works.
* `pytest --collect-only` fails on **37 test modules** with
  `AttributeError: module 'ctypes' has no attribute 'WinDLL'`. Root cause:
  `backend/app/main.py` line ~166 does `credential_store or WindowsCredentialStore()`
  unconditionally, so `create_app()` cannot run off Windows.
* `evidence_store.default_store_directory()` falls back to `~/AppData/Local/Sentinel` when
  `LOCALAPPDATA` is unset, which is wrong on Linux and macOS.
* The README advertises 86 operations, 81 routes, 142 schemas, and 2,200+ tests. The resume
  still says 66 / 61 / 110 / 1,022. Re-count before updating either.

## 5. Phases

Do them in order. Each phase is shippable on its own. Check off acceptance items as you go.

### Phase 0: Boot and test on Linux and macOS (about 3 to 5 days)

Why: nobody outside Windows can try the project, which caps users at near zero.

Tasks:
1. Add a platform neutral credential store behind the existing `CredentialStorePort`:
   * Linux: Secret Service via `keyring` (optional dependency) or a `0600` file under the
     store directory, clearly labeled as weaker in `SECURITY.md`.
   * macOS: `keyring` (Keychain).
   * Choose the implementation in `create_app` by platform, explicitly, with a startup log line
     naming which store is active.
2. Make `default_store_directory()` return `$XDG_DATA_HOME/sentinel` (fallback
   `~/.local/share/sentinel`) on Linux and `~/Library/Application Support/Sentinel` on macOS.
   Keep the Windows path identical. Keep `ensure_store_outside_repository` checks.
3. Guard every module level `ctypes.WinDLL` and `windll` use so importing the module on Linux
   does not crash. Windows only functions should raise a clear `AppError` when called off
   Windows (pattern already exists in `process_supervisor._require_windows`).
4. Report capabilities honestly: on Linux, `process_supervisor` and `appcontainer` capabilities
   are unavailable until Phase 1, and `/health` plus `sentinel` CLI should say so.
5. Passport v2 signing: `passport/cng.py` is Windows only. Off Windows, use a software ES256 key
   stored with `0600` permissions, mark the signer as `software` in the bundle, and keep
   offline verification working. Document the weaker key custody.
6. CI: add an `ubuntu-latest` job (Python 3.12 and 3.14) next to the Windows job.

Acceptance:
- [x] `pytest --collect-only` on Linux reports zero errors.
- [x] Full suite on Linux: all non Windows tests pass, Windows only tests skip with a reason.
      (2026-10-03, Docker python:3.12-slim: 1773 passed, 286 skipped, 0 failed.)
- [ ] Windows CI still green. (Local Windows runs green except the venv-stub recovery test
      noted in section 0; confirm on CI.)
- [x] `sentinel` CLI starts the backend on Linux and creates a Change against a real repo.
- [x] `openapi.json` regenerated if anything in the contract changed.

Phase 0 status (2026-10-03): implemented. Decisions taken, change them if you disagree:
* Credential store kinds `windows | keyring | file`, chosen by `credentials/selection.py`:
  win32 -> windows, darwin -> keyring (dependency there), else -> file (0600 JSON). Override
  with `SENTINEL_CREDENTIAL_STORE`. Never falls back; an unusable store refuses startup.
* Off-Windows Passport signer is `passport/file_key.py`, reported as `SOFTWARE_FILE` (a new,
  additive enum value), never `SOFTWARE`. `passport/keys.py` picks it per platform.
* `process_supervisor` is UNSUPPORTED off Windows; `agent_launcher` stays AVAILABLE with a
  limitation (claude fails closed, generic runs unconfined). `/health` gained `platform` and
  `unsupported_capabilities`.
* Real portability bugs fixed on the way: directory detection in
  `appcontainer.remove_tree_no_follow`, the check-box drive-letter rule, AppContainer PATH
  separator, the trust registry and signer selector paths.
* Linux test harness used: `python:3.12-slim` and `python:3.14-slim` containers fed the
  working tree over stdin (no bind mount), package installed with `pip install -e .`.

### Phase 1: A verified Linux agent boundary (about 2 to 3 weeks)

Why: this is the core security claim. On Windows it is AppContainer plus Job Objects. Linux
needs an equivalent that is verified on the live process, not assumed.

Design (needs kb's decision on item 1 before coding):
1. **Profile resolution without breaking D-01.** D-01 says a boundary is never chosen by
   platform detection or exception handling. Recommended: add `BoundaryKind.LINUX_SANDBOX`
   and let each built in profile declare a per platform boundary explicitly, for example
   `boundaries={"win32": APPCONTAINER, "linux": LINUX_SANDBOX}`. A platform with no declared
   boundary resolves to `UNAVAILABLE` and fails closed. This is still a declared property, not
   a fallback. Alternative: separate adapters (`claude-linux`). Pick one and record it as D-03.
2. **Mechanism:** bubblewrap (`bwrap`) for namespaces (user, mount, pid, ipc, uts, and net
   when the profile has no network capability), a read only bind of the tool snapshot, a read
   write bind of only the Sentinel workspace clone and staged home, `--die-with-parent`,
   `--new-session`, and a seccomp filter denying `ptrace`, `mount`, `unshare` with new
   namespaces, `keyctl`, and `bpf`. Run it inside a dedicated **cgroup v2** subtree with
   `pids.max`, `memory.max`, and `cpu.max` so the whole tree can be frozen
   (`cgroup.freeze`) for pause/resume and killed (`cgroup.kill`) for stop.
3. **Verification before the agent runs** (mirror the Windows "start suspended, verify, then
   resume" flow): start the sandbox with a tiny init that blocks on a pipe; from the
   supervisor, read `/proc/<pid>/ns/*` and confirm each namespace differs from the host,
   confirm the process is in the expected cgroup (`/proc/<pid>/cgroup`), confirm the seccomp
   mode is 2 (`/proc/<pid>/status`), confirm the uid map; only then release the pipe. Any
   mismatch: kill the cgroup and return an `ERROR` run.
4. **Descendant attribution:** poll `cgroup.procs` plus `/proc/<pid>/{stat,exe,cmdline}` to
   fill the existing `DescendantProcess` records (pid, parent, image path, command line,
   lifetime, exit code). Same model as the Job Object path.
5. **Passport:** add `LINUX_SANDBOX` to `passport/boundary.py` only when the verified facts were
   recorded for that launch; otherwise `UNKNOWN`. Decide and document where it sits in the
   weakest ordering (recommended: equal strength class to `APPCONTAINER`, but reported by its
   own name, never relabeled as `APPCONTAINER`).
6. **Confined checks on Linux:** reuse the same sandbox for `check_box.py` runs with network
   always off.

New files (suggested): `execution/linux_sandbox.py`, `execution/linux_supervisor.py`,
`execution/cgroups.py`, `backend/tests/execution/linux/`.

Acceptance (all are automated tests that must pass on `ubuntu-latest`):
- [x] Agent cannot write outside the workspace clone and staged home.
- [x] Agent cannot read `~/.ssh`, `~/.aws`, the Sentinel store directory, or the API token.
- [x] No network when the profile lacks it (connect to 1.1.1.1:443 fails).
- [x] A fork bomb is stopped by `pids.max`; the host stays responsive. (CI `linux-sandbox` job,
      run for `c70b9e5`.)
- [x] Double fork plus `setsid` daemons are still attributed and killed on stop.
- [x] Pause freezes the whole tree (verify CPU time stops increasing, like the Windows test).
- [x] If `bwrap` is missing or user namespaces are disabled, launch fails closed with a clear
      message. No unconfined retry.
- [x] Passport for a Linux run claims `LINUX_SANDBOX` only with recorded verified facts.
- [x] Linux confined check boxes verify their boundary, isolate hostile checks, and bind
      their facts into Passport `confined_checks`; desktop Assurance and Passport show them.

Measure and record in `bench/boundary_overhead/`: median and p95 launch overhead (time from
launch request to agent start) with and without the sandbox, over 200 launches.

### Phase 2: Agent Regression Lab, `sentinel eval` (about 2 weeks)

Why: evals are what AI engineering hiring managers screen for. This turns Sentinel from "a
runtime" into "a way to measure agents," and it reuses everything Sentinel already does.

Task suite format (`evals/tasks/<task_id>/`):
```text
task.toml          # id, title, repo fixture path or git url+sha, prompt, timeout, budget_usd,
                   # allowed_paths, required_checks, hidden_test_command, tags
repo/              # or a pinned git url+sha in task.toml
hidden_tests/      # never visible to the agent
```

Runner flow per task, per attempt:
1. Create a Change from the fixture at the pinned commit.
2. Launch the agent through the normal pipeline (same path as `sentinel run`), with the
   task prompt.
3. After the agent exits, copy `hidden_tests/` into a **fresh confined check box** over the
   result and run `hidden_test_command`. The agent never sees these files.
4. Record: pass or fail, wall time, cost and tokens (parse Claude Code JSON output when the
   adapter provides it, otherwise `UNKNOWN`), number of descendant processes, policy preset
   decision, forbidden path refusals, and the Passport id.
5. Persist results in new tables (additive migration) and journal them.

Statistics (do this properly, it is the part interviewers will poke at):
* Agents are nondeterministic. Run each task `k` times (default 3).
* Report pass rate with a **Wilson 95% interval**, plus mean cost and p50/p95 time.
* `sentinel eval compare <run_a> <run_b>`: per task and overall deltas. Flag a regression only
  when the intervals separate or a paired bootstrap says the drop is significant at 95%. Print
  which tasks flipped.
* `sentinel eval compare --fail-on-regression` exits nonzero, so it works as a CI gate.

Seed the suite with 30 to 50 small, real tasks (bug fixes and small features in tiny Python and
Node repos) with hidden tests. Optionally import a SWE bench Lite subset later.

Acceptance:
- [ ] `sentinel eval run --suite evals/tasks --agent claude --k 3` produces a results report
      (JSON plus a static HTML page).
- [x] Hidden tests are provably absent from the agent's workspace (test asserts it).
      (`evals/test_runner.py`, real-kernel `execution/linux/test_hidden_sandbox.py`.)
- [x] `compare` detects an injected regression (a deliberately broken prompt) and does not
      flag two runs of the same config. (Mock agent, `test_seed_suite_and_cli.py`; exit 3.)
- [x] Unit tests for the Wilson interval and bootstrap against known values.

### Phase 3: Concurrent execution with crash recovery (about 1 to 2 weeks)

Why: "runs one agent" is a demo. "Runs 50 isolated agents at once and survives a crash" is
infrastructure.

1. A persistent job queue in SQLite (`eval_jobs` table: id, task, attempt, state, lease owner,
   lease expiry, attempts). Workers claim jobs by atomically setting a lease.
2. A worker pool with configurable concurrency; each job gets its own Change and workspace
   clone (already isolated per Change).
3. Crash recovery: on startup, jobs with expired leases are requeued; their half finished
   sandboxes and workspaces are swept (extend the existing startup sweep in `workspace/`).
4. Per job budgets: timeout, max cost, max descendant processes.

Acceptance:
- [x] Kill the backend with `SIGKILL` mid run; on restart every job finishes exactly once and
      no orphan processes or cgroups remain. (`evals/test_queue.py::
      test_sigkill_mid_run_then_restart_finishes_every_job_once`.)
- [x] A chaos test kills random workers during a 100 job run and the final report is complete.
      (`evals/test_queue.py::test_chaos_random_worker_kills_during_a_100_job_run`.)

Measure in `bench/concurrency/`: throughput (jobs per hour) and queue delay at concurrency 1,
4, 8, 16 on stated hardware; recovery time after a kill.

### Phase 4: Observability (about 3 to 5 days)

1. OpenTelemetry spans around launch, boundary verification, check runs, and eval jobs, with the
   Change id and run id as attributes. Exporter is off by default; OTLP when configured.
2. A `/metrics` endpoint (Prometheus text format) behind the existing bearer auth: launches,
   boundary failures, eval pass rate, queue depth.
3. A small "Eval" page in the desktop app reading the eval results API (regenerate the client
   with `npm run api:generate`).

Acceptance:
- [x] One eval run produces a connected trace viewable in Jaeger (mock `op-add`, explicitly
      UNCONFINED hidden tests; raw trace and Docker command in `bench/observability/`).

### Phase 5: Adversarial testing (about 1 week)

1. A fuzzer that generates hostile process trees and behaviors: double fork, `setsid`, rapid
   spawn and exit between polls, symlink races into the workspace, attempts to write the
   repository contract, encoded credential exfiltration (extend the existing encodings test),
   attempts to reach the local API.
2. Run it on both Linux and Windows. Every finding becomes a regression test and an entry in
   the threat model findings.

Measure: number of generated scenarios run, escapes found, escapes fixed.

### Phase 6: Release and users (ongoing, start after Phase 2)

1. [x] **License.** Apache 2.0 (decided 2026-10-06; no third-party agreement needed).
2. [x] One command install: `pipx install sentinel-runtime` (name free; release workflow
   ready, first tag pending), and a `sentinel doctor` command that checks bwrap, user
   namespaces, cgroup v2, and Git.
3. README rewrite: [x] 60 second quickstart on Linux, [x] an eval results table at the top,
   [ ] a GIF (removed for now).
4. [x] Public leaderboard: a static page generated from `sentinel eval` results, published with
   GitHub Pages (`leaderboard.yml`; enable Pages with the "GitHub Actions" source).
5. [x] Issue templates and a `CONTRIBUTING.md`; [ ] "good first issue" labels (create on GitHub).
6. Launch: Show HN, r/LocalLLaMA, r/ClaudeAI, agent tooling Discords, and a short write up of
   one interesting finding from the eval data.

Track in `bench/adoption.md`: stars, installs (PyPI downloads), external issues, external PRs.

## 6. Numbers to collect (these become resume bullets)

| Metric | Phase | Where it is produced |
| --- | --- | --- |
| Platforms supported | 0, 1 | CI matrix |
| Boundary launch overhead, p50 and p95 | 1 | `bench/boundary_overhead/` |
| Sandbox escape tests passing | 1, 5 | test report |
| Tasks in eval suite, attempts run | 2 | `sentinel eval` report |
| Pass rate with 95% interval per agent config | 2 | `sentinel eval` report |
| Regression detection: true and false positive rate on injected regressions | 2 | eval tests |
| Throughput and queue delay at N workers | 3 | `bench/concurrency/` |
| Recovery time after crash, jobs lost (target 0) | 3 | chaos test |
| Adversarial scenarios run, escapes found and fixed | 5 | fuzzer report |
| Stars, installs, external contributors | 6 | `bench/adoption.md` |

## 7. Open decisions for kb

1. D-03: how Linux boundary profiles are declared (Phase 1, item 1).
2. Where `LINUX_SANDBOX` ranks in the Passport weakest boundary ordering.
3. ~~License~~: decided, Apache 2.0 (2026-10-06).
4. ~~Upstream or fork~~: decided, development continues independently in this repository
   (2026-10-06).

## 8. Definition of done for this handoff

All six phases merged, CI green on Windows and Linux, `bench/` populated with reproducible
results, and at least 20 external users or 100 stars, whichever comes first.

## Independent fork verification (2026-10-06, continued into UTC 2026-10-07)

Windows full backend collection: **2,444 tests**. The full run returned 2,351
passed, 88 skipped and five failures. All five were corrected and the complete
affected-module rerun returned **153 passed**. Combined coverage therefore has
**2,356 passing cases and 88 platform/opt-in skips**, with no unresolved failure.
The full run was not restarted after these focused corrections.

Corrections from the full run: keep bounded sandbox output capture inside the
execution boundary; explicitly review/pin rustup resolution as a process-starting
execution module; use uv for the unconfined-toolchain refusal regression; update
the portable Passport action's cryptography pin to 50.0.2; and make the recovery
probe wait for the real child PID and prove all observed Job members terminate
without assuming Python launchers create exactly two processes.

Commands: `python -m pytest -o addopts= -v -rs` for the full Windows run;
`python -m pytest -o addopts= -q backend/tests/core/test_subprocess_boundary.py
backend/tests/execution/test_runner.py backend/tests/passport/test_portable_verify.py
backend/tests/recovery/test_git_recovery.py backend/tests/evals/test_hidden.py`
for the affected-module rerun. Local XML/log evidence is under `.tmp/` (ignored).
Desktop: `npm test`, `npm run build`, `npm run test:e2e`, `npm run package:dir`,
and `SENTINEL_SMOKE_REAL_CHECKS=1 npm run test:electron:smoke`.

The final packaged Electron build is running with the isolated profile
`%LOCALAPPDATA%/SentinelFork-verified`. It uses the bundled backend and receives
successful health responses. Existing user data was preserved.

Linux full-suite correction: the host-only logic-test resolver looked
for npm solely in standalone Node layouts. Ubuntu installs npm-cli.js under
`/usr/share/nodejs/npm/bin`; the production Linux resolver already supports it.
The test harness now recognizes the same system layout. This changes no production
isolation rule and still labels the test harness as unconfined host execution.

Linux full backend collection: **2,111 passed, 328 skipped, five failures** out
of 2,444. Those failures are now corrected; the affected-module rerun including
KB end-to-end flows returned **157 passed, one Windows-only skip**. Combined
unique full-suite coverage: **2,116 passing cases and 328 platform/opt-in skips**.
The corrected real Linux kernel suite returned **143 passed, five skips** in
46.76 seconds. No exercised failure remains unresolved. Full collections were
not repeated after focused fixes; original results are retained honestly.

Final summaries: `bench/release/verification.json`; detailed security findings:
`SECURITY_REVIEW.md`; dependency audits: `bench/release/npm-audit.json` and
`bench/release/python-audit.json`. The final application window is Sentinel and
its bundled backend health returned 200. No credentials or API tokens were
included in the saved reports.

## Direct master integration follow-up (2026-10-06 EDT)

The owner requested merging everything directly onto `origin/master`, without a
new branch. The completed local work was committed as `23861ca`. Fetched master
was seven commits ahead (`a47d20a`); merged those existing commits while preserving
both implementations. The remote adds Apache-2.0 LICENSE/NOTICE, sentinel-runtime
package/release metadata, Eval attempts/bootstrap comparisons and TUI, leaderboard
workflow and hidden-runner fixes. These files supersede earlier license/metadata
pending notes; actual external publication is still not established here.

Conflicts resolved in README, historical handoff and hidden-runner/evaluation
code/tests. Retained bounded sandbox capture, reserved-path/link defenses,
failed-workflow handling and existing delegated actors. Also retained the remote's
thread-safe shared-runner preparation, timeout clamping, one-pass prompt rendering,
empty-hidden-file detection fix and fresh delegated verifier fallback when the
attempt has no actor. Requests without either an actor or usable API client refuse.

Post-merge verification: **70 renderer and 108 Electron unit tests**, build/API
consistency/typecheck, **six browser tests** including real delegated verification,
and **14 packaged Electron smoke checks** passed. Backend integration initially
returned 202 passes/two failures: the missing-client guard was corrected, and
reinstalling the renamed project fixed stale environment metadata. Both cases and
their entire affected modules then passed (**28 tests**); combined selected Windows
coverage has **204 passing cases**. Linux integration returned **42 passes** after
refreshing editable package metadata. Real Linux kernel checks returned
**148 passes/five skips**: two resource controllers unavailable, two Go unavailable,
and root permission behavior. This is a focused integration check, not a second
full backend collection. Local logs are `.tmp/merge-*`.

The merge is committed on the existing master branch and sent through a normal
(non-force) push to origin/master. No upstream push, separate branch, tag or package
release is part of this follow-up. Earlier no-commit/no-push statements describe
the implementation phase before this explicit integration request.

## CI #53 desktop failure follow-up (2026-10-06 EDT)

Fetched the actual fork job logs (run 37561401923, desktop job 112599257899).
The serial evidence workflow waited only five seconds for a baseline row while
cold tool discovery was still running. Its serial retry recreated globally stored
actors with the same names, producing a strict-locator error for duplicate Ada
Lovelace actors. The original job reported 105 passing tests, one failure, one
flaky case, one skip and seven unrun serial dependents.

Fixes: captureEvidence now requests a bounded 180-second deadline; the Electron
proxy grants that cap only to POST baseline/current capture routes. GETs and
unknown evidence paths retain 60-second caps, and ordinary requests keep their
15-second defaults. Environment capture probes up to eleven tools, each bounded
at ten seconds. Proxy tests cover both capture routes and negative cases.

The workflow test waits for the actual successful evidence HTTP response before
asserting rendered checkpoints and has an overall bounded capture-flow deadline.
Actors get unique display names per attempt, and selectors/delegations/forks use
the actual actor IDs from creation responses, avoiding retry leftovers and option
loading races. A regression deliberately delays capture 16 seconds and verifies
it succeeds without a Capture failed notice and re-enables the action button.

Validation: 70 renderer plus 108 Electron unit tests, API consistency/typecheck,
production build and a rebuilt packaged Electron application passed. All 14
Electron smoke checks passed. Full browser run: **114 passed, one opt-in skip**
(115 collected); the new slow-capture regression separately **passed**, giving
115 passing cases and one skip over all 116 current cases. No failures or retries
in the full run. Logs are `.tmp/ci53-*`. The owner-authorized direct master push
triggers a new CI run; its remote desktop result is checked separately.

Remote CI #53 confirmed both Linux backend versions and the dedicated kernel
sandbox job passed, with pids/memory/cpu controllers enabled. This supersedes the
prior claim that resource-controller validation lacked remote CI evidence. WSL
still lacks those controllers locally. Windows backend jobs were still running
when this desktop fix was prepared.

## CI #54 packaging follow-up (2026-10-06 EDT)

Run 37563558121 confirmed the browser fix remotely: **115 passed, one opt-in
skip** in 7.7 minutes, without failures or flaky cases. It then reached the
previously unexecuted staging step and failed with empty signature status/subject.
The old signature subprocess did not check launch/exit errors or expose stderr.
The observed output does not establish a bad or unsigned Python download: the
pinned official archive SHA-256 matched. Signature verification remains mandatory.

The staging helper now uses the absolute Windows PowerShell host, removes
inherited PSModulePath entries (including differently cased names), imports that
host's built-in Microsoft.PowerShell.Security module explicitly, and passes its
literal-path script through UTF-16LE EncodedCommand. It runs noninteractively with
errors set to Stop, a 30-second deadline and hidden window. Structured JSON carries
status and publisher. Launch errors, nonzero exits, malformed output, unsigned
files and wrong publishers all fail closed with actionable diagnostics. This
isolates module/quoting differences between PowerShell 7 CI and Windows PowerShell.
No signature or checksum verification is bypassed.

Added regression tests for system-host selection, encoded literal quoting,
module-path removal and every refusal case. **110 Electron unit tests passed**;
a real signature check returned Valid/Python Software Foundation. The complete
local package rebuild passed, and **all 14 Electron smoke checks passed** again.
Only staging/policy/tests and CI ordering changed after the successful remote
browser run. Package build/smoke steps now run before the long browser soak so
future packaging failures surface earlier. A normal direct-master push triggers
another remote validation run. Logs: `.tmp/ci54-package-fixed.log`,
`.tmp/ci54-electron-fixed.log`, `.tmp/ci54-signature-tests.log`.

All four backend jobs from #53 (Windows/Linux, Python 3.12/3.14) and its dedicated
Linux kernel sandbox job subsequently completed successfully. The original #53
failure was confined to desktop, and #54's browser stage is now green.
