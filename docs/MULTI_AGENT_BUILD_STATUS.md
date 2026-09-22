# Multi-agent coordination — build status

Source contract: `docs/MULTI_AGENT_IMPLEMENTATION_PLAN.md` (and the build prompt in
`docs/CLAUDE_MULTI_AGENT_BUILD_PROMPT.md`). This file is the live decision log,
phase tracker, and backend-to-frontend handoff record required by that plan.
Update it at every phase boundary; do not mark a phase done without recorded
test evidence.

Branch: `feature/multi-agent-coordination` (created off `master`; nothing here
is merged to `master` and nothing is pushed).

## How to resume this work

1. Read this file's "Current state" and "Next concrete action" sections first.
2. Read `docs/MULTI_AGENT_IMPLEMENTATION_PLAN.md` section 14 for the full phase
   order — only Phase 0 and Phase 1 are addressed so far.
3. Run the focused test commands listed under "Test evidence" to confirm the
   recorded state before adding new work; do not assume it still holds.
4. Continue at the next incomplete phase. Do not restart completed phases.

## Current state

- Phase 0 (contracts and baseline): **done**.
- Phase 1 (durable tasks and dependency graph): **backend + CLI done, tests
  passing**. No frontend slice yet (see "Frontend status" below — this is a
  known, explicitly tracked gap, not an oversight).
- Phases 2-7: **not started**.

## Next concrete action

Start Phase 2 (isolated workspaces and immutable results) per plan section 14,
or, if frontend parity is prioritized first, build the "Tasks and graph"
frontend slice (plan section 17 feature integration map) against the Phase 1
API before moving on. Both are legitimate next steps; neither has been started.

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
