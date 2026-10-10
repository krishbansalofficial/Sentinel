# Sentinel: Handoff for the Next Build Phase

Owner: Krish Bansal (kb). Fork: `krishbansalofficial/Sentinel` (upstream `csshlok/Sentinel`).
Written: 2026-10-03. Status updated 2026-10-09. Read this whole file before touching code.

## 0. Status at a glance (read first)

| Phase | State | Where |
| --- | --- | --- |
| 0. Boot and test on Linux/macOS | **Done**, pushed | commits `cd1bf89`..`8236ca1` |
| 1. Verified Linux agent boundary | **Done**, including Linux confined check boxes; CI confirmation for this continuation pending | `0811441`..`c70b9e5` |
| 2. `sentinel eval` | **Done**: stats, suite, runner, drivers, CLI (`run/compare/report`, `--record`), HTML report, 30-task seed suite, backend store + API + journal events | `5e439dc`..`f10f8e2` |
| 3. Queue and crash recovery | **Done**: queue, pool, fencing, SIGKILL + chaos tests, bench, `eval run --workers/--queue/--resume` | `997406e`, `5ad8598` |
| 4. Observability | **Done**: optional OTLP spans (`[telemetry]` extra), connected Jaeger trace, `/api/v1/metrics` (hardened: counts only Sentinel's own refusal lines), desktop Eval page with run comparison | `75962e0`, `b00ad7b`, `a934b9f`, `4c6a6e4` |
| 5. Adversarial fuzzer | **Linux done** (40 scenarios, 153 behaviors, 0 escapes); Windows seeded AppContainer fuzzer passes 12 scenarios / 48 behavior attempts, 0 observed escapes | `bench/fuzz/`, `9cc2d40`, `42aea0a`; Windows child-spawn attempts were refused on this host |
| 6. Release and users | **Partial**: doctor, contribution docs/templates, README quickstart + honest mock eval table, static leaderboard CLI + preview. Remaining: agreed license, package registration, real Claude results, GIF, Pages publication, launch | `bench/release/`; PyPI metadata check returned 404, no name reservation |

Verification for `b00ad7b`..`42aea0a` (2026-10-09): Windows targeted suites (core, evals, policy, launcher, contract, AppContainer fuzz) green after the fixes in those commits; Linux full suite 2024 passed with the only failures being the README count and CI pin tests fixed in `42aea0a`/`b00ad7b` and telemetry tests needing the `[telemetry]` extra (86/86 green with it); desktop typecheck, api:check and 65 unit tests green. Docker is only the Windows dev box's way to reach a Linux kernel for tests; Sentinel never uses Docker at runtime.

### Linux confined check boxes: implemented
The Linux platform in `execution/linux_check_box.py` supplies the existing `BoxPlatform`
seam: private 0700 storage, Linux identities, scratch HOME/TMPDIR, and verified bubblewrap,
seccomp and cgroup execution. Runtime binds are read-only: Python's venv/base install,
Node's prefix and project node_modules, and offline GOROOT. Unsafe runtime roots and
cargo/dotnet/uv are refused. Linux has no Windows runtime snapshot cache.

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
* CI: check the post-push runs, especially linux-sandbox; local controller skips
  do not establish that job's success.

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
1. **Phase 2 leftovers**: tests for `SandboxHiddenTestRunner` (Linux) and `VerificationHiddenTestRunner` (Windows);
   a first real `--agent claude` run (needs a Claude login) to fill the eval results table.
   Ubuntu 24.04 hosts need `packaging/apparmor/sentinel-bwrap` (read its trade-off note).
2. **Done: Linux confined check boxes** (Phase 1 item 6): verified Linux platform,
   read-only runtimes, journaled Linux facts, Passport confined_checks and desktop views;
   verification and controller limitations are recorded above.
3. **Phase 5/6 follow-up**: extend Windows scenarios to race junctions and exercise successfully
   spawned hostile descendants on a suitable host; review `bench/fuzz/windows-results.xml`.
   Review `bench/release/leaderboard-smoke.html`; obtain license agreement, select the package
   name, collect real agent results/GIF, then publish and launch. No public release was made.

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
- [ ] Hidden tests are provably absent from the agent's workspace (test asserts it).
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
- [ ] Kill the backend with `SIGKILL` mid run; on restart every job finishes exactly once and
      no orphan processes or cgroups remain.
- [ ] A chaos test kills random workers during a 100 job run and the final report is complete.

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

1. **License.** Required before calling it open source. The repo is shared with csshlok; agree
   on a license with him first (Apache 2.0 recommended for the patent grant).
2. One command install: `pipx install sentinel-runtime` (check the PyPI name), and a
   `sentinel doctor` command that checks bwrap, user namespaces, cgroup v2, and Git.
3. README rewrite: 60 second quickstart on Linux, a GIF, an eval results table at the top.
4. Public leaderboard: a static page generated from `sentinel eval` results, published with
   GitHub Pages.
5. Issue templates, a `CONTRIBUTING.md`, and "good first issue" labels.
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
3. License, agreed with csshlok.
4. Whether to upstream this work to `csshlok/Sentinel` or keep it in the fork. Keep your own
   commits clearly attributable either way, since this phase is your individual contribution.

## 8. Definition of done for this handoff

All six phases merged, CI green on Windows and Linux, `bench/` populated with reproducible
results, and at least 20 external users or 100 stars, whichever comes first.
