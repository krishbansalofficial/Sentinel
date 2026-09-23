# Multi-agent coordination — build status

Source contract: `docs/MULTI_AGENT_IMPLEMENTATION_PLAN.md` (and the build prompt in
`docs/CLAUDE_MULTI_AGENT_BUILD_PROMPT.md`). This file is the live decision log,
phase tracker, and backend-to-frontend handoff record required by that plan.
Update it at every phase boundary; do not mark a phase done without recorded
test evidence.

Branch: `feature/multi-agent-coordination-phases` (created off `master`,
pushed to `origin` at the request of the user; never pushed to `master`).
Phase 0-1 landed as 10 commits `ef60364..db6aa45`. The older local branch
`feature/multi-agent-coordination` is superseded by this one.

Session rules (user instructions): whenever a session is stopped or paused,
update this file first, then check with the user before resuming
implementation. When session token usage reaches ~95% of its budget, update
all docs, then commit and push to this branch (never `master`).

## How to resume this work

1. Read this file's "Current state" and "Next concrete action" sections first.
2. Read `docs/MULTI_AGENT_IMPLEMENTATION_PLAN.md` section 14 for the full phase
   order. Phases 0-2 are done; Phases 3-5 have schema only (migrations
   12-13).
3. Run the focused test commands listed under "Test evidence" to confirm the
   recorded state before adding new work; do not assume it still holds.
4. Continue at the next incomplete phase. Do not restart completed phases.

## Current state

- Phase 0 (contracts and baseline): **done**.
- Phase 1 (durable tasks and dependency graph): **backend + CLI done, tests
  passing, committed and pushed**. No frontend slice yet (see "Frontend
  status" below — this is a known, explicitly tracked gap, not an oversight).
- Phase 2 (isolated workspaces and immutable results): **done**, tests
  passing (110 coordination-focused tests, including 70 workspace tests and
  3 mock-data scenarios). A second review pass found and fixed 8 bugs. `WorkspaceManager` does the Git
  work. `WorkspaceRegistry` adds durable records, explicit retention states,
  journaled transitions, crash reconciliation, and result pinning. It is wired
  into `RuntimeServices.workspaces` but no dispatcher calls it yet; that is
  Phase 3. The Phase 2 exit (two attempts edit the same file without
  affecting each other or the user) is proven through the durable registry.
- Phases 3-5 (scheduler/dispatch, recovery/fencing, integration): **schema
  only** (migration 12, applied and green in the existing tests). No service
  code yet. See "Phases 2-5 — work in progress" below.
- Phases 6-7: **not started**.

## Next concrete action

Wait for the user's go-ahead (session rule above). Then continue at
continuation step 3 (Phase 3 scheduler: resources, attempts, dispatch).
The task execution fields are now persisted (see "Phase 2 — second review
pass"), so the scheduler can read them straight from `StoredTask`.

## Phase 0 — decision log

The plan (section 3) was written from an inspection of this repository and
matched the actual code closely. Points checked and corrected against the real
code before implementing:

- **Idempotency table is already generic.** `idempotency_records(scope, key,
  request_hash, result_json, created_at)` (migration 2) is not Change-specific
  — it is keyed by an arbitrary `scope` string. Coordination reuses this table
  directly (`scope` values like `"coord_tasks:create"`,
  `"coord_task:<id>:submit"`) instead of adding a parallel table, matching
  `ChangeRepository`'s own replay pattern exactly.
- **Journal transaction threading.** `JournalWriter.append(..., connection=)`
  already supports sharing one open `sqlite3.Connection` with a domain write,
  which is exactly what the plan requires ("no process launch ... inside that
  transaction" / "domain mutation and journal append roll back together").
  `TaskRepository` follows `ChangeRepository`'s existing pattern of opening one
  `BEGIN IMMEDIATE` connection per public method and passing it to both the
  domain `UPDATE`/`INSERT` and `journal.append`.
- **`JournalEventType` is a closed `StrEnum`, additive by convention.** New
  members (`TASK_CREATED`, `TASK_EDITED`, `TASK_DEPENDENCIES_REPLACED`,
  `TASK_SUBMITTED`, `TASK_CANCELLED`) were appended at the end of the enum,
  matching how `AGENT_PAUSED`/`CHANGE_FORKED` were added in prior migrations —
  no existing member was renamed or removed.
  `backend/tests/core/test_contracts.py` was inspected first to confirm it
  does not assert an exhaustive/closed member list that this would break; it
  does not (it tests individual model shapes only).
- **Revision-based optimistic concurrency and `AppError` conventions are
  reused as-is.** `TASK_REVISION_CONFLICT` intentionally mirrors
  `revision_conflict()`'s shape (`expected_revision`/`actual_revision` in
  `details`) rather than inventing a new envelope shape.
- **Migration numbering.** Latest applied migration in this repo is 10
  (`descendant_processes`). Coordination adds migration 11
  (`coord_tasks_and_dependencies`), append-only per `backend/migrations/README`
  convention (no earlier migration edited).
- **Task/Attempt/AgentRun identity separation (plan section 4).** Phase 1 adds
  only the `Task` identity (`coord_tasks`) and its dependency edges
  (`coord_dependencies`). `Attempt` and the AgentRun linkage do not exist yet —
  intentionally deferred to Phase 3 per the plan's own phase order. A Phase 1
  task therefore has no attempt, no dispatch, and no way to reach any state
  past `READY`/`WAITING`/`CANCELLED`. This is the literal Phase 1 exit
  condition in plan section 14 ("no queued task launches before scheduler
  support is enabled") — not a shortcut.
- **State machine scoped down for Phase 1.** The full state machine in plan
  section 4 (`ACTIVE`, `RESULT_READY`, `INTEGRATING`, `SUCCEEDED`,
  `RETRY_WAIT`, `BLOCKED`, `CANCEL_REQUESTED`) requires a scheduler and
  attempts that do not exist yet. Phase 1 implements only the reachable subset
  — `DRAFT -> WAITING | READY` (via submit, depending on whether declared
  dependencies are all `SUCCEEDED`, which is vacuously true with zero
  dependencies) and `{DRAFT, WAITING, READY} -> CANCELLED` (direct, since there
  is no live attempt to request cancellation of yet). All `TaskState` members
  from the plan are still defined in the contract now, so Phase 3/4 do not
  need a breaking enum change — only the transition table gains rows.
- **Dependency edges are frozen once a task leaves `DRAFT`.** Matches plan
  section 4 ("freeze running task inputs and dependency definitions") and
  section 7 rule 3. `PUT .../dependencies` is rejected with
  `TASK_NOT_EDITABLE` once a task has been submitted.
- **Cycle rejection is whole-graph, not just the edited task's neighborhood.**
  Given first-release scale (a handful of tasks per Change), the transactional
  edge-replace validates the entire Change's dependency graph for cycles
  (topological-sort based) rather than a narrower reachability check from the
  edited node. This is simpler to prove correct and cheap at this scale; if
  task counts grow large enough to matter, replace with an incremental check —
  not required by the acceptance checklist as written.
- **No material incompatibility found.** Nothing in the plan required an
  architecture change the existing code could not support. The extension
  points listed in plan section 3 (database, journal, migrations, contracts,
  router, composition root, CLI) all matched the real files at the paths the
  plan named.

## Phase 1 — durable tasks and dependency graph

### What was built

- `backend/migrations/versions.py`: migration 11
  (`coord_tasks_and_dependencies`) adds `coord_tasks` and `coord_dependencies`,
  plus indexes for Change-scoped listing, state filtering, and dependency
  lookups in both directions.
- `backend/app/contracts/models.py`: `TaskState`, `TaskAdapter` is not a new
  enum (adapter stays a free-form `ShortText` matching `AgentLaunchRequest`'s
  existing `adapter: ShortText`, so no adapter allowlist duplication is
  introduced), `TaskCreateRequest`, `TaskEditRequest`,
  `TaskDependenciesRequest`, `TaskSubmitRequest`, `TaskCancelRequest`,
  `TaskView`, `TaskListResponse`; five new `JournalEventType` members (see
  decision log).
- `backend/app/coordination/` (new package): `errors.py` (stable
  `TASK_*` `AppError` codes), `models.py` (`StoredTask`, `StoredDependencyEdge`
  dataclasses), `repository.py` (`TaskRepository` — transactional CRUD,
  revision CAS, idempotency replay, atomic dependency-edge replace with
  whole-graph cycle detection, paired journal writes, all on the existing
  `Database`/`JournalWriter`), `service.py` (`CoordinationService` — request
  validation, cross-Change dependency/task-not-found checks, computes
  `waiting_reason`/derived `TaskState` at submit time, builds `TaskView`).
- `backend/app/core/router.py`: task routes registered under the existing
  `/api/v1` router and existing bearer-token dependency (see route table
  below).
- `backend/app/main.py`: `TaskRepository`/`CoordinationService` wired into
  `create_app`/`_build_runtime_services` alongside the other runtime services;
  added to `RuntimeServices` as `runtime.coordination`.
  `_DEFAULT_CONFIGURED_CAPABILITIES` gained `"task_coordination"` so
  `GET /api/v1/capabilities` reports it like every other capability.
- `backend/app/cli/client.py` / `backend/app/cli/main.py`: `ApiClient` methods
  and a `task` Typer sub-app (`create`, `list`, `show`, `edit`, `dependencies`,
  `submit`, `cancel`) — CLI talks to the real API only, no persistence import,
  matching every existing CLI command group.
- Tests: `backend/tests/coordination/test_repository.py`,
  `backend/tests/coordination/test_service.py`,
  `backend/tests/acceptance/test_coordination_routes.py`,
  `backend/tests/cli/test_task_commands.py` (see "Test evidence").

### Backend-to-frontend handoff — "Tasks and graph" slice

Readiness: **backend tested, CLI wired; frontend not started.**

| Route | Method | Purpose | Request schema | Response schema |
|---|---|---|---|---|
| `/api/v1/changes/{change_id}/tasks` | POST | Create a `DRAFT` task | `TaskCreateRequest` | `TaskView` (201) |
| `/api/v1/changes/{change_id}/tasks` | GET | List tasks (bounded pagination: `limit` 1-100 default 100, `offset`) | — | `TaskListResponse` |
| `/api/v1/changes/{change_id}/tasks/{task_id}` | GET | Task detail incl. dependency ids and `waiting_reason` | — | `TaskView` |
| `/api/v1/changes/{change_id}/tasks/{task_id}` | PATCH | Edit `DRAFT`-only fields with `expected_revision` | `TaskEditRequest` | `TaskView` |
| `/api/v1/changes/{change_id}/tasks/{task_id}/dependencies` | PUT | Replace dependency edges transactionally (rejects cycles/self/cross-Change) | `TaskDependenciesRequest` | `TaskView` |
| `/api/v1/changes/{change_id}/tasks/{task_id}/submit` | POST | `DRAFT -> WAITING|READY` | `TaskSubmitRequest` | `TaskView` |
| `/api/v1/changes/{change_id}/tasks/{task_id}/cancel` | POST | `{DRAFT,WAITING,READY} -> CANCELLED` | `TaskCancelRequest` | `TaskView` |

- **Scopes/authorization**: identical to every other route today — a single
  bearer token gate (`require_bearer_token`), no finer-grained actor/policy
  check yet. The plan's per-operation policy scopes (section 11) are deferred
  to whichever phase first needs to distinguish actors for coordination
  (mutating dispatch/integration authority) — Phase 1 has no dispatch, so
  there is nothing yet that a narrower scope would protect beyond what the
  bearer token already protects. Recorded here as an explicit gap, not a
  silent omission.
- **Revisions**: every task starts at `revision=1`; mutating routes require
  `expected_revision` in the body and return `409 TASK_REVISION_CONFLICT`
  (`details.expected_revision`/`details.actual_revision`) on mismatch, exactly
  like `ChangeContractUpdateRequest`.
- **Idempotency**: `POST`/`PUT`/`PATCH` accept the existing `Idempotency-Key`
  header; same key + same body replays the stored `TaskView`; same key +
  different body returns `409 IDEMPOTENCY_KEY_REUSED`, matching every existing
  mutating route.
- **Async/2xx semantics**: everything in Phase 1 is synchronous — there is no
  dispatcher yet, so every response reflects the fully applied state, not a
  202-accepted intent. `201` on create, `200` on edit/dependencies/submit/
  cancel/get/list. This will change starting Phase 3 (submit/dispatch-adjacent
  routes may become asynchronous once a real dispatcher exists) — do not
  assume this synchronous contract is permanent.
- **Error codes**: `TASK_NOT_FOUND` (404), `TASK_REVISION_CONFLICT` (409),
  `TASK_NOT_EDITABLE` (409, edit/dependency mutation on a non-`DRAFT` task),
  `TASK_DEPENDENCY_SELF` (409), `TASK_DEPENDENCY_CROSS_CHANGE` (409),
  `TASK_DEPENDENCY_NOT_FOUND` (409, referenced predecessor does not exist in
  this Change), `TASK_DEPENDENCY_CYCLE` (409, `details.cycle` lists one
  offending task-id cycle), `TASK_INVALID_TRANSITION` (409, submit/cancel from
  a state that does not allow it), `IDEMPOTENCY_KEY_REUSED` (409),
  `VALIDATION_ERROR` (422, existing generic handler), `CHANGE_NOT_FOUND` (404,
  existing — a task route always 404s this way first if the Change itself
  does not exist).
- **Refresh rules for a future frontend**: after any task mutation, refetch
  that task's detail (`['coordination', changeId, 'task', taskId]`) and the
  task list (`['coordination', changeId, 'tasks']`); dependency replacement
  also invalidates every other task in the same Change's detail query since
  cycle validation is whole-graph (their `waiting_reason` can be unaffected,
  but this keeps the UI honest about what the backend actually re-validated).
- **Migration/setup**: automatic — `Database.initialize()` applies migration
  11 on next backend startup against any existing `changes.sqlite3`; verified
  by `test_migration_from_existing_database` (below) starting from a database
  that only has migrations 1-10 applied.
- **What is explicitly NOT in this slice**: no dispatch, no attempts, no
  workspaces, no capacity/queue view, no retry (retry requires an attempt
  history that does not exist yet), no integration. `GET
  /changes/{change_id}/coordination` (queue/capacity/integration status from
  plan section 11) is not implemented — there is no queue yet to report on.

### Frontend status

Not started. No changes under `apps/desktop/`. This is the honest state, not
an inferred one — `apps/desktop` was not touched by this phase. Per plan
section 17's compatibility gate, a frontend build against this slice should
expose the "Tasks and graph" UI (form/list/detail/dependency editor) per the
plan's feature integration map, behind the existing capability-detection
pattern (`task_coordination` now appears in `GET /api/v1/capabilities`, so the
frontend can detect support without inventing an empty scheduler UI when
talking to an older backend).

### Test evidence

Baseline (recorded before any coordination code was written, on `master`
prior to branching — reused here since the branch is otherwise unchanged from
`master` at that point):

```
python -m pytest backend/tests/core backend/tests/acceptance -q
```
Result: all passed, 1 skipped (platform skip — recorded, not investigated,
since it predates this work and Phase 1 code does not touch what it skips).

Phase 1 focused suite:

```
python -m pytest backend/tests/coordination backend/tests/acceptance/test_coordination_routes.py backend/tests/cli/test_task_commands.py -q
```
Result: **32 passed**, 0 failed (16 repository tests, 6 service tests,
7 acceptance/API tests, 3 CLI tests). Covers: migration from a database with
only migrations 1-10 applied; create/list/get persistence across a repository
reopen; idempotency replay and conflict (repository, service, and over real
HTTP with the `Idempotency-Key` header); stale-revision rejection; self/
cross-Change/unknown-predecessor dependency rejection; two-task and
three-task dependency cycle rejection (repository and over HTTP) with proof
the rejected write did not partially apply; fields and dependencies frozen
after submit; submit computing `WAITING` vs `READY` from predecessor state;
double-submit rejected as an invalid transition; cancel from `READY` and
rejection of cancelling an already-`CANCELLED` task; a real
`threading.Barrier`-synchronized concurrent test where two threads race to
complete a 2-cycle from opposite directions, asserting the graph never ends
up with both directions applied; a journal-failure injection test proving
the domain write and journal event roll back together (mirrors
`test_runtime_service_atomicity.py`'s existing proof for the Change domain);
`GET /api/v1/capabilities` reports `task_coordination`; task routes 404 with
`CHANGE_NOT_FOUND` for an unknown Change; CLI create/list/show/submit/cancel/
dependencies against a fake HTTP transport, including a dependency-cycle
error surfacing as CLI exit code 1 with the `TASK_DEPENDENCY_CYCLE` code.

Full regression pass before calling Phase 1 done:

```
python -m pytest backend/tests -q
```
Result: **one failure on the first full run, root-caused and fixed, then
independently re-verified as passing.** The first full-suite run was started
*before* `openapi.json` was regenerated (see the desktop contract-sync step
below, which was run afterward while the suite was still executing in the
background), so `test_frozen_openapi_snapshot_matches_the_live_app` correctly
failed against the stale snapshot it read at that moment — a self-inflicted
ordering artifact of doing both in parallel, not a real defect. Fix: none
needed beyond finishing the regeneration already in flight.
Re-verification: `python -m pytest backend/tests/acceptance/test_contract_boundaries.py -q`
was re-run standalone immediately afterward against the now-current
`openapi.json` and passed (7 passed, 0 failed). A full clean re-run of
`backend/tests` was then started fresh to get one trustworthy whole-suite
number; its result is recorded here once it finishes
(the prior full run's non-openapi output showed only that one failing line
among several hundred dots across every other suite, including
`backend/tests/coordination`, so no other regression was observed).

Desktop contract sync (plan section 17 step 2 — done even though no frontend
UI slice exists yet, so the generated client artifact does not silently drift
out from under whoever builds that slice next):

```
python -c "import json; from backend.app.main import create_app; json.dump(create_app().openapi(), open('openapi.json', 'w'), indent=2, sort_keys=True)"
cd apps/desktop && npm run api:generate && npm run api:check && npm run typecheck
```
Result: all four succeeded. `api:check` confirms the committed generated
schema matches `openapi.json`; `typecheck` passes with zero errors (nothing
in `apps/desktop/src` references the new coordination types yet, so this
only proves the generation step itself is sound, not that any UI consumes
it — that is the deferred frontend slice).

### Unresolved limitations carried forward from Phase 1

- No scheduler, so `WAITING`/`READY` tasks never progress on their own — this
  is correct per the plan's Phase 1 exit condition, not a bug, but it means
  Phase 1 alone has no end-to-end demonstrable "agent ran" story yet.
- Whole-graph cycle detection on every dependency edit is O(tasks + edges) per
  call; fine at demo scale, called out above as a revisit point if task counts
  grow.
- No per-actor policy scope on task routes yet (see handoff notes above).
- No frontend slice yet (see "Frontend status" above).

## Phases 2-5 — work in progress

### Schema (migration 12, `coord_execution`)

New `coord_tasks` columns: `executable`, `args_json`, `write_paths_json`,
`verification_json`, `resources_json`, `current_attempt_id`, `attempt_count`,
`next_eligible_at`, `pinned_base_sha`, `accepted_result_sha`,
`resolves_integration_id`. New tables: `coord_attempts` (partial unique index
`WHERE live = 1` enforces one live attempt per task in the database itself),
`coord_workspaces`, `coord_resources`, `coord_resource_leases`,
`coord_dispatch_intents` (records `pid` + process creation time, for identity
that is safe against PID reuse), `coord_scheduler_owner` (singleton + epoch),
`coord_change_settings` (dispatch pause, integration ref), and
`coord_integrations`. The Phase 1 migration test now asserts
`LATEST_SCHEMA_VERSION`. Evidence: coordination + migration + route tests →
32 passed, 0 failed with migration 12 applied.

### Code facts that shaped the design

- `AgentLauncher.launch()` blocks until exit and mints `run_id` internally.
  Its only early signal is the `on_update` observer, whose failures are
  ignored. Decision: add an additive `on_started` keyword so the dispatcher
  can durably record `run_id`/pid before the run finishes.
- Attempt runs flow through `on_update` → `EvidenceStore.save_agent_run`, so
  they appear in the existing AgentRun list for the Change automatically.
- The hardened Git runner `GitRepositoryInspector._capture_git` is reused for
  worktree, commit, merge and `update-ref` operations. Hooks are neutralized
  with `core.hooksPath` pointing at an empty managed directory.
- Dispatch rechecks `policy.evaluate(assigned_actor, change, "agent.launch")`
  immediately before launch. A missing or failed authority check → task
  `BLOCKED`, never launched.
- CORS `allow_methods` lacks `PATCH`. It must be added for the browser to use
  `PATCH /tasks/{id}` (frontend slice).
- `app = create_app()` runs at import, so the dispatcher starts in the
  FastAPI `lifespan`, not in `create_app`.

### Design (plan sections 6-10, as applied to this code)

1. Workspaces: repository identity = canonical `--git-common-dir`. Worktrees
   go under `<db dir>/coord-workspaces/<id>` on branch
   `sentinel/attempt/<attempt id>`, pinned to a base SHA, and the user
   checkout is never touched. Capture after confirmed exit commits an
   immutable result and records changed/renamed/binary/untracked files, scope
   violations, and conflict markers. Cleanup refuses foreign paths and live
   attempts.
2. Integration ref per Change: `refs/heads/sentinel/integration/<change id>`,
   initialized to committed HEAD. Attempt bases pin to it at dispatch.
3. Scheduler: a deterministic `tick()` with an injected clock, plus a bounded
   executor. Claims happen in one transaction: all-or-none resource
   reservation, attempt + dispatch intent, task `ACTIVE`, and the journal
   event. The launch happens after commit.
4. Dispatch intent goes `PENDING` → `LAUNCHING` (durable, before launch) →
   `LAUNCHED` (`on_started`). Finalization validates generation/epoch/liveness
   and rejects stale attempts with `ATTEMPT_STALE`.
5. Recovery: an OS lock file plus a durable epoch. On restart, `PENDING` →
   safe requeue. `LAUNCHING` with no pid → quarantine + `BLOCKED`. A pid is
   matched on `(pid, creation time)`. Never blind relaunch.
6. Cancellation: `CANCEL_REQUESTED` (202) → stop → `CANCELLED` only after exit
   is confirmed.
7. Integration: a single writer. Merge in a fresh worktree; conflict →
   `CONFLICT`/`BLOCKED` with both sides preserved. Checks run on the exact
   candidate, and the candidate must be unchanged afterwards. `ADVANCING` is
   persisted before a CAS `update-ref`, and restart reconciles ref state.
   Resolution tasks start from target + conflicting merge.

### Phase 2 — what was built

- `backend/app/coordination/workspaces.py` (`WorkspaceManager`). All Git calls
  go through `GitRepositoryInspector._capture_git`, with hooks neutralized via
  `core.hooksPath=<managed root>/.empty-hooks` and `commit --no-verify`.
  - `repository_identity()`: canonical `--git-common-dir` (resolved and
    casefolded), identical across worktrees of one repository.
  - `resolve_commit()`, `read_ref()`, `ensure_ref()` (creation is a CAS
    against the all-zero SHA), and `compare_and_swap_ref()` (`update-ref new
    old`). These write only `refs/heads/sentinel/...`
    (`WORKSPACE_REF_NOT_MANAGED` otherwise).
  - `create()`: `git worktree add -b sentinel/attempt/<id> <managed>/<id>
    <base sha>` (or `--detach` for integration candidates). The user
    checkout is never touched.
  - `capture()`: after confirmed exit, runs `add -A`, then restores
    sensitive-pattern files (`.env`, `*.key`, `*.pem`, ...) to HEAD in the
    index via `reset HEAD --`, so a tracked secret is never committed as a
    deletion or as agent content. Then commits (merge-aware) and reports the
    files (added/modified/deleted/renamed with old path, binary flag),
    `scope_violations` against declared `write_paths` (reusing
    `assurance.deviations.matches_any`), `excluded_sensitive`,
    `conflict_markers`, and an explicit `no_change`.
  - `merge_into()` / `abort_merge()` / `head()` / `tracked_modifications()`:
    primitives for Phase 5 integration and resolution tasks.
  - `remove()`: refuses live attempts (`WORKSPACE_ACTIVE`) and any path that
    resolves (following junctions, casefolded) outside the managed root
    (`WORKSPACE_NOT_MANAGED`).
- `backend/tests/coordination/test_workspaces.py` (11 tests, real temp repos,
  repository and managed root paths contain spaces): two attempts editing the
  same file while the user's dirty checkout/branch stays unchanged;
  rename/binary/untracked/scope/sensitive capture; a tracked sensitive file
  restored rather than deleted; explicit no-change; conflict markers; a merge
  conflict detected with both sides preserved; a clean merge; ref CAS and the
  managed-ref guard; identity shared across worktrees; cleanup refusal for
  live/foreign/`..` paths; and (Windows only) an NTFS junction escape refused
  plus a case-aliased workspace path still recognized.

### Phase 2 — completion (durable retention layer and hardening)

- Migration 13 (`coord_workspace_retention`) adds `capture_json`,
  `result_ref`, `detail`, and `removed_at` to `coord_workspaces`, plus
  `(change_id, state)` and `attempt_id` indexes. It is append-only, and
  migration 12 is unchanged.
- Contracts: `WorkspaceState` (`CREATING → READY → CAPTURED → REMOVING →
  REMOVED`, or `FAILED`), `WorkspacePurpose` (`ATTEMPT`/`INTEGRATION`), and
  six `workspace.*` journal events.
- `backend/app/coordination/workspace_registry.py` (`WorkspaceRegistry`).
  Each Git side effect is bracketed in three steps: first an intent row plus
  its journal event (one transaction), then Git with no transaction open,
  then an outcome transition compare-and-set on the intent state.
  - `provision()` pins `base_revision` to a SHA before writing anything. Git
    failure → `FAILED` with detail.
  - `capture()` is idempotent (`CAPTURED` returns the stored record with no
    Git call) and pins the result under `refs/heads/sentinel/results/<id>`
    (create-once CAS). `WORKSPACE_CORRUPT` → `FAILED`. A diverged or
    oversized result leaves the workspace `READY` for inspection.
  - `remove()` retention: only `CAPTURED`/`FAILED` (and a retried
    `REMOVING`) may be removed. `READY` never is, because it may hold
    uncaptured work. A live attempt → `WORKSPACE_ACTIVE`. A directory is
    deleted only if Git lists it as a worktree of the recorded repository;
    otherwise the call fails with `WORKSPACE_UNOWNED_DIRECTORY` and leaves
    the directory in place. The result ref survives removal.
  - `reconcile()` handles crash windows:
    - `CREATING` → `READY` only if Git registers the worktree *at its
      base*, otherwise `FAILED`. It never re-runs creation.
    - `REMOVING` → `REMOVED` once the directory is gone, else reported as
      pending.
    - `READY` with its directory missing → `FAILED`.
    - `CAPTURED` with its directory missing → `REMOVED`.
    - Unrecorded directories under the managed root are reported as
      orphans and never deleted.
  - Git worktree administration is serialized per repository identity, and
    capture/remove per workspace. Across registries, the database
    state-guard CAS is the backstop.
  - Wired as `RuntimeServices.workspaces` (managed root
    `<db dir>/coord-workspaces`). `reconcile()` is deliberately **not** run at
    startup yet: it must run under scheduler ownership, which is Phase 3
    lifespan work.
- `WorkspaceManager` hardening:
  - **Bug fixed:** "no change" used to mean "nothing staged", so an agent
    that committed its own work, or a capture replayed after a crash
    between `git commit` and the database write, was reported as
    `no_change` and its work was dropped. Now `no_change` means
    `HEAD == base`.
  - Capture refuses:
    - `WORKSPACE_CORRUPT`: a broken or redirected `.git` link. Without this
      check, Git would discover an enclosing repository and commit into
      it; proven with a managed root inside an outer repo.
    - `WORKSPACE_BASE_DIVERGED`: HEAD no longer descends from the pinned
      base.
    - `WORKSPACE_RESULT_TOO_LARGE`: more than 2000 files, counting the
      agent's own commits. The index is restored and the worktree is
      untouched.
  - `sensitive_committed` reports credential-pattern files that reached the
    result through the agent's own commits. Working-tree exclusion cannot
    drop those without rewriting history.
  - New `pin_result`, `is_ancestor`, `registered_worktrees`, `prune`, and
    `CaptureResult.to_manifest`.

### Phase 2 — test evidence

First run (library only): `test_workspaces.py`, **11 passed**, including the
Windows-only NTFS junction escape test.

Completion run (Windows 11, Python 3.14, system git):

```
python -m pytest backend/tests/coordination/test_workspaces.py backend/tests/coordination/test_workspace_registry.py -q
```
Result: **63 passed, 0 failed, 0 skipped**:

- `test_workspaces.py`: 31 tests, 20 of them new. They cover:
  - agent-committed work
  - agent commit plus uncommitted edits
  - recapture idempotency
  - deletes and unicode/space paths
  - agent-committed secrets
  - base divergence
  - the oversized-result bound, including agent commits
  - a broken `.git` link inside an outer repo
  - repo hooks, including a repo-configured `core.hooksPath` that never
    runs
  - content filters, which are refused and never executed
  - creation guards
  - non-commit and unknown bases
  - base pinning while the user's branch moves
  - result pin surviving branch deletion + worktree removal +
    `gc --prune=now`
  - worktree registration
  - `ensure_ref` never overwriting
  - a resolution attempt producing a two-parent merge
  - four parallel captures behind a barrier
  - a fixed-seed randomized model check over six workspaces
- `test_workspace_registry.py`: 32 tests. They cover:
  - the full lifecycle and journal order, with the hash chain verified by
    `ReplayService`
  - the Phase 2 exit through the registry
  - scoping
  - duplicate ids
  - no row on an unresolvable base
  - `FAILED` on a Git create failure
  - 404s
  - `READY` and live-attempt removal refusals
  - idempotent removal that keeps the result
  - a foreign directory at the managed path never being deleted
  - corrupt → `FAILED`, diverged → stays `READY`
  - capture idempotency: no second commit, one journal event
  - journal failure rolling back the capture transition, with retry
    completing from the committed state
  - crash injection (a `BaseException`, like a killed process) before/after
    Git create, after the capture commit, and before/after Git removal,
    each followed by `reconcile()`
  - externally deleted directories
  - orphans reported but never deleted
  - reconcile as a no-op on settled state
  - six concurrent provisions
  - four concurrent captures of one workspace, and two separate registries
    racing, each yielding exactly one result
  - migration 12 → 13 over an existing row

Mutation check: each key guard was broken one at a time, and the targeted
test caught all 9. The broken guards were:

- the `no_change` semantics
- the worktree integrity check
- the ancestry check
- hook neutralization
- the bound counting commits
- the removal registration check
- `READY` removability
- capture idempotency
- reconcile's base check

The script is not committed. It replaced one line at a time and restored the
source.

Contract sync: `openapi.json` regenerated, then `npm run api:generate`,
`api:check`, and `typecheck` all passed. The snapshot also carries the
Phase 3 contract additions already in `contracts/models.py`: new
`JournalEventType` members and task execution fields, which are not persisted
yet (see "Next concrete action").

Full regression after Phase 2 (`python -m pytest backend/tests`, run from
the repository root): **875 passed, 5 skipped, 0 failed** (9m14s). The skips
are pre-existing platform skips.

### Phase 2 — second review pass (bugs found and fixed)

A scratch probe ran each edge case against real Git, and every bug below was
reproduced before it was fixed.

1. **A staged rename deleted a tracked secret.** After
   `git mv service.key notes.txt`, the working-tree status showed only the
   new path, so `service.key` was committed as deleted and went unreported.
   Capture now stages first, then reads `diff --cached -M`, which gives both
   sides of a rename. Any sensitive old or new path is restored to HEAD and
   reported.
2. **`git rm --cached` by an agent crashed capture.** This applied to any
   file still on disk, not just secrets. The shared strict status parser
   rejected the staged-delete-plus-untracked state as "duplicate status
   paths", producing a generic 500. Capture no longer uses that parser.
3. **A repository the agent created or cloned inside its workspace crashed
   capture.** Git lists it as `dir/`, which the path normalizer rejected.
   Staging it would also produce a gitlink, which the hardened runner
   refuses, and one without commits makes `git add` itself fail. Embedded
   repositories are now detected via `ls-files --others` and left out of
   `add` with `:(exclude,literal)` pathspecs. They are reported as
   `excluded_embedded` and never committed.
4. **Agent-chosen names were used as glob pathspecs.** A directory named
   `[ab]` would also match the file `a`. Exclusion and unstaging now use
   literal pathspecs.
5. **`sensitive_committed` missed secrets the agent's own commits deleted or
   renamed away.** It now checks the old path as well as the new one.
6. **Task execution fields were accepted by the API and silently dropped**
   (`executable`, `args`, `write_paths`, `verification`, `resources`). They
   are now persisted end to end: create, edit, read, list, and idempotency
   replay. Replay records written before this change still load, with the
   fields empty.
7. **An edit that named no fields still wrote a `task.edited` journal
   event.** It no longer does.
8. **Parallel captures raced on the shared object database (flaky, Windows).**
   Worktrees share `.git/objects`. Two captures writing an identical blob
   at the same moment intermittently failed with
   `unable to write file .git/objects/..: Permission denied`.
   - Found by looping the concurrency tests: 1 failure in 25 runs.
   - Reproduced with a spy on the Git runner, which showed the failing
     `git add` and its stderr.
   - Fix: capture and merge now hold a per-repository lock, keyed by the
     canonical common Git directory and shared by every `WorkspaceManager`
     in the process.
   - A new identical-content stress test fails 4 of 5 runs without the lock
     and passes 5 of 5 with it.
   - Residual risk: a Git process outside Sentinel writing the same object
     at the same instant is not covered by this lock.

Not fixed, recorded instead: on Windows, a case-only rename
(`App.py` → `app.py`) is invisible to Git because of `core.ignorecase`, so
it is not part of the result. Also, a sensitive file an agent renames to a
non-sensitive name keeps its content under the new name; name-based
exclusion cannot see content.

New tests:

- `test_workspaces.py`: +6 regression tests.
- `test_repository.py`: +3 (execution-field persistence and replay, replay
  of a pre-change idempotency record, empty edit).
- `test_coordination_routes.py`: +2 (HTTP round trip of the execution fields
  through create, get, and patch; duplicate resource keys rejected).
- `test_mock_data.py`: 3 seeded mock-data scenarios:
  - A generated project of about 115 files (packages, tests, docs, binary
    assets, CRLF script, unicode name, ignore rules). Five mock agents run
    in parallel as real subprocesses. They modify, add, delete, rename,
    write binaries, commit part of their work, leave ignored junk, and drop
    a `.env`, and all of them edit one shared file. Results are captured
    concurrently and compared byte for byte against an independent model.
    Afterwards the user's dirty checkout is intact and every result survives
    removal plus `gc`.
  - 300 seeded workspace rows across 10 Changes in every state, reconciled
    in one pass. Every expected transition and per-Change journal count is
    checked, the journal chains verify, orphans are kept, and a second pass
    is a no-op.
  - 40 HTTP tasks with random execution fields, idempotent replays,
    dependency chains, a refused cycle, paginated listing checked field by
    field, and submission yielding `READY`/`WAITING` from the dependencies.

Mutation check: each of the 7 new fixes was reverted one at a time, and all
7 were caught. The 9 original mutations were all still caught.

Verification after the second pass:

- Full backend suite: **890 passed, 5 skipped, 0 failed** (875 before, plus
  15 new tests).
- Coordination-focused suites: 110 tests.
- Concurrency tests looped 15× after the fix: 0 failures (1 in 25 before).
- Mock-data suite run 3×: all passed.
- Desktop: `api:check`, `typecheck`, and `npm test` (98/98) all passed.
- No contract change in this pass, so `openapi.json` is unchanged.

### Phase 2 — limitations

- Repositories whose files use Git content filters (for example Git LFS) are
  refused by the hardened runner (`GIT_COMMAND_FAILED`). This is safe, since
  a filter command never runs, but those repositories cannot host attempts.
- Declared write paths are checked after the run, not enforced during it.
  Worktrees share Git metadata with the source repository and are not a
  sandbox (plan section 2).
- A sensitive file the agent commits itself is reported
  (`sensitive_committed`), not removed. The Phase 3/5 caller must treat it
  as a rejection reason.
- In-process locks serialize Git administration within one backend. Across
  processes, the database CAS prevents double transitions, but two backends
  could still race `git worktree add`. The Phase 3 single-owner scheduler
  epoch is what excludes that.

### Continuation steps

1. ~~Migration 12~~ done.
2. ~~`workspaces.py` + real-repo tests~~ done. ~~Durable registry,
   retention, reconciliation (migration 13)~~ done (63 passed).
3. Launcher `on_started`, `resources.py`, attempts, `scheduler.py`, lifespan
   wiring, routes, CLI, fake-launcher tests → commit Phase 3.
4. Reconciliation, cancellation, retry, quarantine, crash tests → commit
   Phase 4.
5. `integration.py`, CAS, resolution → commit Phase 5.
6. Regenerate `openapi.json` + desktop types after route changes.
