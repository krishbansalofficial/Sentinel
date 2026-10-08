# Contributing to Sentinel

Thanks for helping. Sentinel supervises AI coding agents, so correctness of its claims matters
more than features. Read `HANDOFF.md` (section 2) before your first change.

## Ground rules (these are tested)

1. **Fail closed.** If a boundary cannot be established and verified, the launch fails. Never fall
   back to a weaker boundary on an exception.
2. **Unknown is never PASS.** Missing or unverifiable evidence is `UNKNOWN`.
3. **The API contract is additive.** Change a route or model, then regenerate `openapi.json` and the
   desktop client in the same commit (`HANDOFF.md` has the commands).
4. **Migrations are additive.** Add a numbered migration; never edit an old one.
5. **Processes start only in `backend/app/execution/` or `backend/app/git/safe_exec.py`**, each
   spawning module pinned with a reason (`backend/tests/core/test_subprocess_boundary.py`).
6. **Tests with every change.** Platform-specific tests skip with a reason; real-boundary tests
   carry a host positive control so "it was denied" cannot be vacuously true.
7. **Honest numbers.** Benchmarks live in `bench/` with the exact command, hardware and raw output.

## Setup

```bash
python -m pip install -e ".[test,tui,keyring]"
sentinel doctor            # what this machine can and cannot do, with fixes
python -m pytest -q
cd apps/desktop && npm ci && npm test
```

On Linux the real sandbox tests need bubblewrap and a delegated cgroup v2 subtree; see
`backend/tests/execution/linux/conftest.py` and the `linux-sandbox` CI job.

## Good first issues

Look for the `good first issue` label: small, well-specified changes with a test to write first.
