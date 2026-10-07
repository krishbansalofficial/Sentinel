# Fork security review — 2026-10-06

This continuation reviewed execution boundaries, evaluation integrity, desktop
packaging and dependencies. It is a source review with local adversarial tests,
not a certification or an exhaustive penetration test.

## Findings addressed

| Finding | Change | Evidence |
| --- | --- | --- |
| Cargo and .NET checks had no confined adapter | Added Windows SDK snapshots, private caches and offline execution; Linux SDK installations are bound read-only | Real Windows Rust and SDK-only .NET programs compiled and ran inside AppContainer. Host-positive-control file reads, writes and loopback connections succeeded outside the box and were denied inside it |
| Windows hidden verification omitted its delegated actor and used the wrong request shape | Delegated verification and apply scopes; send the authenticated verification contract; require APPCONTAINER evidence for a hidden-test pass | Contract validation, fake-client regression tests and a real API acceptance test |
| An agent could plant the reserved hidden_tests path before the privileged copy | Refuse existing destinations and linked/reparse-point hidden-test sources; use a fresh destination | Link and reserved-path regression tests |
| Failed agent attempts and interrupted workflows could still reach hidden checks | Reject non-passing agent outcomes, interrupted workflows and known budget overruns; bind task contracts and Claude's budget flag | Evaluation regressions; real provider quota failure recorded as ERROR |
| Linux hidden-test output used unbounded communicate | Drain with bounded capture and a 4,000-byte tail; close the sandbox and remove failed-launch cgroups | Noisy subprocess, timeout, incomplete-capture and unverified-boundary tests |
| Packaged backend staging could include local evidence data and used incomplete dependency metadata | Filter private stores, credentials and databases; derive pinned runtime requirements from pyproject.toml; quote PowerShell paths safely | Packaging policy tests and a rebuilt, self-contained Electron application |
| Dependency audit found 12 npm vulnerabilities and four advisories for cryptography 47.0.0 | Updated vulnerable build dependency chains and pinned cryptography 50.0.2 | Saved npm and OSV Python audit reports in bench/release; zero known advisories in the final scans |
| The desktop bundle used CPython 3.12.10 | Updated to CPython 3.14.8 with the published SHA-256 and an independent Authenticode check | Download matched the official checksum; rebuilt package and Electron smoke checks |
| Packaged Python checks could not build a runtime snapshot and pytest was absent | Validate the official embedded stdlib layout, replace its path configuration in check snapshots, load only confined dependencies and package pytest | A real pytest check through the packaged Electron bridge passed under APPCONTAINER and denied host file reads/writes and access to the backend port |
| The inherited pytest 8.4.2 pin had vulnerable temporary-directory handling | Pin pytest 9.0.3 for runtime verification and development tests | Final OSV scan of all 33 packaged dependencies reports no known advisories |

| Verification dialog sent the obsolete flat payload without a delegated actor | Select an actor, validate its UUID and send the wrapped verification contract; preserve quoted arguments | Real browser dialog runs a Python check with PASSED/APPCONTAINER |
| Long confined checks inherited short HTTP/IPC deadlines | Apply bounded execution deadlines only to known POST execution routes, retaining normal read deadlines | Proxy deadline tests and real browser/Electron checks |
| Packaged assurance lacked its coverage collector | Include pinned coverage 7.16.2 in runtime dependencies | Packaged diff coverage measured 100% for the changed executable line inside AppContainer |

The Python audit queries the [OSV API](https://google.github.io/osv.dev/api/).
Cryptography publishes its [security advisories](https://github.com/pyca/cryptography/security/advisories).
Audit results describe the scanned versions and the advisory database at scan time.
The bundled interpreter comes from the [Python 3.14.8 security release](https://www.python.org/downloads/release/python-3148/).
Pytest's fix is documented in its [9.0.3 release](https://github.com/pytest-dev/pytest/releases/tag/9.0.3).

## Validation and limits

The renderer unit suite, Electron unit suite, TypeScript/API consistency checks,
production build and 113 browser tests passed. The full browser run passed 112;
the dialog stress test exceeded its 30-second deadline and passed on rerun
with a 60-second deadline for its 80 animated dismissals. The packaged application passed
12 startup, bridge, secret-exposure and graceful-shutdown checks plus real
confined pytest and diff-coverage checks. Enable the latter with `SENTINEL_SMOKE_REAL_CHECKS=1` on
a Windows host supporting AppContainer. Real captures
are in `bench/release/walkthrough/` and `bench/release/walkthrough.gif`.
The full backend collections covered 2,444 cases per platform. All failures
were corrected and passed in affected-module reruns (153 Windows, 157 Linux).
The final Linux kernel suite passed 143 with five documented skips. See
[HANDOFF_FORK.md](HANDOFF_FORK.md) and `bench/release/verification.json` for
original results, corrected results and limits.

The authenticated Claude attempt reached a verified AppContainer but the provider
rejected it for weekly quota exhaustion. It does not establish model accuracy.
The WSL host uses hybrid cgroups. A private mount namespace exposes its existing
cgroup v2 mount for local isolation, attribution, freeze and cleanup tests without
changing host mounts. This harness disables unavailable resource controllers;
memory and process-count enforcement tests therefore skip. Production requires
those controllers and retains its fail-closed behavior. The dedicated Linux CI
job must verify them; its new remote run remains outstanding.

Cargo dependencies must be vendored; .NET restore requires SDK-only dependencies
or a local feed. The .NET acceptance test uses a console harness and does not
exercise NuGet's external testhost packages. pnpm/yarn and Windows Server retain
the limitations documented in SECURITY.md. Generic agents remain weaker than
verified AppContainer or Linux sandbox launches. Same-user processes, a compromised
host, signing-key protection and upstream licensing retain their documented limits.
SDK snapshots retain the installed host SDK versions. The saved dependency scans
cover packaged Python libraries and the npm tree, rather than the host OS and SDK binaries.

No successful descendant-spawn containment claim is added: this Windows host
refuses those adversarial spawn attempts. Zero observed escapes in the exercised
cases does not prove that all possible escapes are prevented.
