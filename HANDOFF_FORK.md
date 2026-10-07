# Independent fork session handoff

Updated: 2026-10-06 EDT / 2026-10-07 UTC. Workspace: `C:\Users\krish\Sentinel-fork`.

Krish owns this continuation and authorized implementation, security fixes,
testing, packaging and launching Electron. The former hackathon/team approval
requirements are historical and superseded. Preserve upstream attribution and
license obligations. No commits, pushes, publication or external messages were
made in this session.

## Changes and reasons

| Area | Fix and evidence |
| --- | --- |
| Cargo/.NET checks | Implemented confined SDK snapshots on Windows and read-only SDK binds on Linux. Cargo uses private offline caches, explicit rustc/rustdoc and native MSVC/Windows SDK linker inputs; .NET uses private CLI/NuGet caches, offline restore and disabled telemetry, diagnostics and workload resolution. Real Windows Go, Rust and SDK-only .NET probes passed. |
| SDK discovery | Resolve Rustup proxies in a fresh directory with a minimal environment, ignoring repository toolchain selection. Validate complete installations and refuse links/unsafe runtime roots. Architecture tests explicitly pin this reviewed process probe. |
| Evaluation authority | Send a delegated actor and wrapped verification body; bind task contracts, Claude budget flags and relevant scopes. Failed agents, stopped workflows, known budget overruns and unverified checks cannot become successful hidden-test results. |
| Hidden-test isolation | Refuse reserved hidden-test paths and links/reparse points before staging fresh trees. Bound Linux output capture and tails, clean failed-launch cgroups and require verified confinement. Output capture stays inside the execution module. |
| Desktop verification | Repair the obsolete request contract; add actor selection/UUID validation and preserve quoted command arguments using a shared parser. A real browser dialog produced PASSED/APPCONTAINER and stdout `5`. |
| Execution deadlines | Allow bounded runtime preparation plus command deadlines for known execution POST routes only. Ordinary read deadlines stay short; invalid deadlines fall back safely. Unit tests cover route and bound behavior. |
| Packaging privacy | Exclude stores, tokens, databases, local credentials, tests and caches. Derive exact runtime dependencies from pyproject, validate staged metadata and safely quote PowerShell paths. New packaging-policy tests passed. |
| Embedded Python | Update CPython to 3.14.8, verify the published hash and Python Software Foundation signature. Validate embedded stdlib archives, replace unsafe path configuration and load confined dependencies/project imports in snapshots. |
| Bundled assurance | Include pytest 9.0.3 and coverage 7.16.2. Packaged pytest denied host reads/writes and backend-loopback access; real diff coverage measured 100% of the changed executable line in AppContainer. |
| Dependencies | Replace cryptography 47.0.0 with 50.0.2, synchronize the portable Passport action pin and repair vulnerable npm build chains. Saved npm and OSV audits report zero known advisories; Python audit covers 33 packaged libraries. |
| Electron smoke | Remove inherited ELECTRON_RUN_AS_NODE, bound fetch/CDP requests, fail on early exit, clear pending requests on socket closure and clean owned child trees. Optional real checks use a safe temporary Git repository. All 14 checks passed on the final rebuild. |
| Frontend loading | Move walkthrough metadata and command parsing into shared small modules, avoiding eager imports of lazy pages. Build, API consistency and TypeScript checks passed. |
| Browser stress | Trace showed 80 animated dismissals exceeded the default 30-second test deadline while continuing to progress. Set a 60-second stress-test deadline; rerun passed with no DOM leak/focus failure. |
| Recovery regression | Wait for the actual spawned child PID rather than a Python launcher shim; verify the top-level process and every observed descendant terminate. Accept intermediate launcher members in the reported termination count. All 19 recovery tests passed. |
| Linux test harness | Recognize Ubuntu's `/usr/share/nodejs/npm/bin/npm-cli.js` in the host-only logic harness, matching the existing production Linux resolver. This adds no production host fallback. Final Linux affected-module rerun passed. |
| CI/docs | Add Windows desktop verification/build/package/smoke CI, remove obsolete implementation approval requirements, document honest confinement and provider limits, capture actual Electron screenshots and GIF. Remote CI has not been run by this session. |

## Validation

Renderer unit tests: **69 passed**. Electron unit tests: **108 passed**.
Browser suite: **112 passed** in the full run plus **1 passed** on the corrected
dialog stress-test rerun, covering all 113 collected cases. Real verification
was enabled with `SENTINEL_E2E_REAL_CHECKS=1`.

Windows backend full run: **2,351 passed, 88 skipped, five failures** out of 2,444.
All five failures were corrected; the final affected-module rerun returned
**153 passed**. Combined unique passing cases: **2,356**, with no unresolved
Windows failure. Do not describe the original full run as wholly green.

Linux kernel acceptance: **143 passed, 5 skipped**, including real Python/Node
checks, hostile cases, attribution, freeze and cleanup. WSL has hybrid cgroups;
the test harness privately binds its existing v2 mount and disables unavailable
resource controllers. Two skips concern memory/process limits, two native
architecture and one root permissions. Production still requires controllers
and fails closed. A suitable host or dedicated CI must validate resource limits.

Full Linux: **2,111 passed, 328 skipped, five failures**; final affected rerun:
**157 passed, one Windows-only skip**. Combined unique passing cases: **2,116**.
The final kernel acceptance rerun also passed **143 tests with five skips**.
No exercised failure remains unresolved.
Machine-readable session results are in `bench/release/verification.json`. Local test XML/logs live in ignored `.tmp/`; saved public-safe dependency
and provider reports plus genuine screenshots/GIF live in `bench/release/`.

## Running and reproducing

Use Node 24+ and Python 3.12+. The final test environment is
`.tmp/security-suite-env/Scripts/python.exe` (Python 3.14.3). Packaged Python is
3.14.8. Install development dependencies with `pip install -e '.[test,tui,keyring,telemetry]'`.
In `apps/desktop`, run `npm ci`, `npm test`, `npm run build`, `npm run test:e2e`,
`npm run package:dir`, and `npm run test:electron:smoke`. Set
`SENTINEL_SMOKE_REAL_CHECKS=1` for bundled pytest/diff-coverage checks on a usable
Windows AppContainer host. Before direct Electron launch remove the inherited
`ELECTRON_RUN_AS_NODE` variable. Executable:
`apps/desktop/release/win-unpacked/Sentinel.exe`.

The final desktop is left open using isolated profile
`%LOCALAPPDATA%/SentinelFork-verified`; backend health returns 200. `.tmp/desktop-launch.json`
records its PID/profile. Preserve existing user data and unrelated processes.

Backend full command: `python -m pytest -o addopts= -v -rs`.
Affected rerun includes `backend/tests/core/test_subprocess_boundary.py`,
`backend/tests/execution/test_runner.py`, `backend/tests/passport/test_portable_verify.py`,
`backend/tests/recovery/test_git_recovery.py`, `backend/tests/evals/test_hidden.py`,
and, on Linux, `backend/tests/kb_flow/test_kb_end_to_end.py`.

## Remaining external limits and next-session cautions

The authenticated Claude CLI launched with reduced authority in AppContainer,
but weekly quota exhaustion prevented a successful task. Saved result is ERROR
with no hidden check or Passport; no model-accuracy claim is supported. See
`bench/release/claude-local*`. Do not retry against an exhausted provider quota.

Package registry publication, Pages hosting, release posts and remote CI are
external release actions still outstanding. .NET was tested with an SDK-only
console harness, not NuGet's external testhost. Linked pnpm/yarn installations,
Windows Server, generic agents, same-user threats and host/key compromise retain
the limits in SECURITY.md/SECURITY_REVIEW.md. Zero observed escapes is no proof
against all escapes. Windows hostile descendant spawn attempts were refused;
do not claim successful adversarial descendant containment from them.

Original user edits were already present in `e2e/reallife.spec.ts`,
`src/features/checks/CheckRunsSection.tsx` and untracked `e2e/checks.spec.ts`.
Preserve them. The repo remains dirty with this session's work; review the diff
before making any commit. No subagents were used.

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
