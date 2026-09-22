# Sentinel multi-agent coordination implementation plan

Status: proposed implementation, not shipped functionality.
Prepared: 2026-09-22. Repository paths below were inspected for this plan; recheck them before implementation.

## 1. Product outcome

Extend Sentinel from supervising individual coding agents into coordinating many tasks with isolated edits, bounded execution capacity, controlled integration, and independently recorded evidence.

A user creates a Change, defines tasks and dependencies, chooses concurrency limits, and starts execution. Sentinel gives each attempt a workspace, schedules eligible tasks, supervises agent processes, collects immutable results, verifies combined changes, and records the complete coordination history in the Change Passport.

Primary demonstration: three logical agent tasks share two execution slots. Two modify overlapping code in separate worktrees. Sentinel detects an integration conflict, preserves both outputs, and accepts only a verified resolution. A separate crash scenario proves stale attempts cannot submit accepted results or silently cause duplicate execution.

## 2. Scope and guarantees

### First release

- Local Windows, one operator, one authoritative backend scheduler.
- Existing generic, Claude, and Codex launcher adapters; no dependency on a particular model vendor.
- Durable task graph, bounded worker pool, one fresh process invocation per attempt.
- One managed Git worktree and branch per attempt.
- All-or-nothing acquisition of declared resources, including worker capacity.
- Leases, scheduler epochs, fencing, bounded retries, cancellation, and startup reconciliation.
- One integration writer per repository and a dedicated integration branch.
- Verification bound to an exact immutable candidate commit.
- CLI and desktop controls; TUI task visibility and essential controls.
- Coordination events, replay support, and passport provenance.

### Later extensions

- Cooperative checkpoint/resume within a task where an adapter explicitly supports it.
- Reusable isolated workers, provider request/token rate limiters, and reliable usage accounting.
- Semantic conflict prediction and symbol-level scheduling hints.
- Competing implementations and an explicit evaluation/selection policy.
- Remote workers, containers, or stronger OS isolation as separately designed trust boundaries.

### Explicit limits

- Worktrees separate normal edits but share Git metadata and are not a filesystem or network sandbox.
- Sentinel currently does not intercept every descendant tool call or independently observe all filesystem writes. Declared file scopes are planning and post-run validation inputs, not enforced filesystem permissions.
- Fencing protects operations routed through Sentinel's validating service. It cannot stop a process with direct filesystem access from writing arbitrary files.
- Deadlock freedom applies to the scheduler's declared resource acquisition protocol. It does not establish deadlock freedom for arbitrary subprocesses, databases, external services, or human dependencies.
- A task timeout or process suspension does not prove resources are released.
- Do not promise exactly-once process launch or arbitrary external side effects. Use durable intents, idempotent controlled operations, and reconciliation of uncertain outcomes.

## 3. Existing architecture and extension points

| Existing path | Role | Planned integration |
|---|---|---|
| `backend/app/execution/launcher.py` | Supervised launch, in-memory run state, update callbacks | Add a durable orchestration adapter around launch; retain current supervision |
| `backend/app/execution/process_supervisor.py` | Windows process-tree supervision | Confirm attempt termination and reconcile process ownership |
| `backend/app/core/evidence_runtime.py` | Change-scoped policy checks for execution | Follow authorization pattern for task, attempt, and integration actions |
| `backend/app/core/database.py` | SQLite WAL and explicit transaction boundaries | Atomic claims, resource reservation, task transitions, paired journal writes |
| `backend/app/core/journal.py` | Hash-chained event/effect journal | Versioned coordination events using the same transaction as domain mutations |
| `backend/migrations/versions.py` | Ordered migrations | Append migrations without changing historical definitions |
| `backend/app/contracts/models.py`, `ports.py` | Public types and protocol boundaries | Add additive coordination models and ports |
| `backend/app/main.py` | Composition and backend lifecycle | Start/stop dispatcher and reconciliation service |
| `backend/app/core/router.py` | API routing | Register coordination routes through existing authentication boundary |
| `backend/app/git/` | Repository inspection and Git evidence | Reuse hardened execution/path patterns for managed worktrees |
| `backend/app/verification/`, `assurance/` | Checks and assurance | Run checks against pinned integration candidates |
| `backend/app/passport/builder.py` | Passport assembly | Add versioned task/attempt/integration provenance |
| `backend/app/cli/` and `backend/app/tui/` | Operator interfaces | Task commands and task status/control surface |
| `apps/desktop/src/features/agents/AgentsTab.tsx` | Change agent view | Link observed runs to task attempts |
| `apps/desktop/src/features/workspace/AgentsPage.tsx` | Workspace agent view | Aggregate scheduled, active, and blocked work |
| `openapi.json`, `apps/desktop/scripts/api/generate.mjs` | API snapshot and generated types | Regenerate snapshot/types and preserve old operations |

Important existing gap: the launcher's observer is not a durable transaction boundary. Its comments explicitly allow callback failures without affecting a run. Coordination correctness must not depend only on that callback. Introduce durable dispatch intent and explicit finalization/reconciliation; use callbacks for bounded telemetry updates.

Proposed new package: `backend/app/coordination/` containing `models.py`, `repository.py`, `service.py`, `scheduler.py`, `resources.py`, `workspaces.py`, `dispatcher.py`, `reconciliation.py`, `integration.py`, and `errors.py`. These are responsibility boundaries, not a requirement to create empty scaffolding up front.

## 4. Task and attempt model

A task is a durable logical objective. An attempt is one execution of it. An AgentRun is the existing observation of a launched invocation. Keep these identities separate so retries preserve history.

Task fields:

- ID, Change ID, title, instructions, creator actor, assigned actor, adapter.
- State, revision, priority, enqueue sequence, creation/update times.
- Required resources, declared read/write paths, verification requirements.
- Maximum attempts, execution timeout, total task deadline, optional budget.
- Input artifact references and pinned base commit.
- Current attempt ID, accepted result ID, structured waiting/failure reason.

Attempt fields:

- ID, task ID, attempt number, run ID, dispatch intent ID.
- Scheduler epoch, fencing generation, lease expiration, last heartbeat.
- Workspace ID, immutable base commit, result commit/digest.
- State, start/end times, execution deadline, cancellation request time.
- Structured failure code, retry eligibility, bounded diagnostic references.

Task state machine:

```text
DRAFT -> WAITING -> READY -> ACTIVE -> RESULT_READY -> INTEGRATING -> SUCCEEDED
                      ^       |             |              |
                      |       v             v              v
                      +-- RETRY_WAIT      BLOCKED        BLOCKED

Nonterminal states -> CANCEL_REQUESTED -> CANCELLED
Exhausted or permanent failures -> FAILED
```

Use `WAITING` for unmet dependencies, `READY` for execution eligibility, and queue reason fields for unavailable resources. `BLOCKED` means an actionable condition such as a merge conflict or uncertain process termination. Define allowed transitions in one table and reject invalid transitions with a stable conflict error.

Attempt states: `RESERVED`, `STARTING`, `RUNNING`, `STOP_REQUESTED`, `COMPLETED`, `FAILED`, `LOST`, `CANCELLED`. A `LOST` attempt is not automatically safe to replace; resource records can remain quarantined pending reconciliation.

For implementation tasks, `SUCCEEDED` means their result was integrated and required checks passed. Read-only tasks may succeed with validated artifacts. A zero agent exit code alone never means task success.

Dependencies must name their completion condition. MVP: implementation dependencies wait for `SUCCEEDED`; artifact-only tasks can depend on a validated artifact. Pin dependent inputs to predecessor result digests and the accepted integration head at dispatch. Do not start dependents against an unrelated old base.

## 5. Persistence design

Append migrations for these logical tables; use existing UUID/time/JSON conventions.

| Table | Essential data and constraints |
|---|---|
| `coord_tasks` | Task fields; Change foreign key; revision for compare-and-swap updates |
| `coord_dependencies` | Task and predecessor IDs; unique edge; reject self-edge and cross-Change edge |
| `coord_attempts` | Attempt fields; unique `(task_id, attempt_number)`; at most one live attempt per task |
| `coord_workspaces` | Canonical path, repository identity, branch/ref, base SHA, attempt owner, cleanup state |
| `coord_resources` | Canonical key, capacity, next generation, quarantine state |
| `coord_resource_leases` | Resource, attempt, units, generation, epoch, expiration; unique owner/resource |
| `coord_artifacts` | Attempt, artifact kind, content digest, immutable storage reference, validation state |
| `coord_integrations` | Result ID, expected target SHA, candidate SHA, check result refs, state, generation |
| `coord_dispatch_intents` | Attempt, command fingerprint, state, execution identity, reconciliation metadata |
| `coord_scheduler_owner` | Singleton owner, epoch, renewal time, ownership expiration |

Use indexes for ready-task selection, active attempts, expired leases, pending integration, and journal correlation. Choose actual table names after checking for collisions.

Atomic domain transition pattern:

1. Open a short `BEGIN IMMEDIATE` transaction.
2. Validate current task revision, owner epoch, authorization facts, and resource availability.
3. Update task/attempt/resource rows and append the journal event using the same connection.
4. Commit, then perform external work.

Never keep a SQLite transaction or scheduler mutex open across agent launch, Git commands, tests, network calls, or human waits. Domain rollback must also roll back its corresponding journal event.

Mutating API operations require a scoped idempotency key and payload digest. Same key/same payload returns the original operation; same key/different payload returns a conflict. A retry cannot mint a second attempt accidentally.

## 6. Scheduling, multiplexing, and fairness

The first implementation multiplexes logical tasks over execution slots. It does not swap hidden model state between arbitrary processes.

Configuration defaults for a demo: two execution slots, one verification slot, one integration writer per repository, three attempts per task, bounded execution timeout. Make these configurable and treat them as initial policy choices rather than benchmark results.

Dispatcher algorithm:

```text
on wakeup or bounded periodic tick:
    verify scheduler ownership and epoch
    reconcile expired/uncertain attempts
    advance dependencies and retry timers
    select eligible tasks by priority + aging, then enqueue sequence
    for each candidate:
        transaction:
            revalidate eligibility and authority
            reserve ALL requested execution resources or reserve NONE
            create attempt and dispatch intent
            transition task and append journal event
        dispatch committed intent outside transaction
```

Use a dedicated worker pool or managed executor for the blocking launcher. API handlers return durable operation/task IDs promptly. A free slot must not mean a blocking FastAPI request waiting for an agent to finish.

Priority aging increases effective priority as a task waits; cap user priority and preserve deterministic FIFO ties. Bound retries with exponential backoff and jitter. Provide a reservation/draining policy for a large resource request that repeatedly loses to small requests; strict starvation freedom still depends on finite task duration, available capacity, and admission policy.

Admission rejects impossible requests, such as three worker units when total capacity is two. Report waiting reasons: dependency, execution capacity, resource ownership, retry delay, integration conflict, authority expiry, or uncertain termination.

Do not infer provider token spend from process lifetime. First release enforces concurrency and wall-clock budgets; token/cost usage is unknown unless a trusted accounting adapter supplies it. A provider-process cap is not a provider API rate limiter because one process can make many requests.

## 7. Deadlock prevention protocol

Managed resources have canonical exact keys, such as `worker:global`, `repo:<identity>:integration`, `db:<instance>`, and `port:<number>`. For MVP, prefer isolated per-attempt databases/ports and avoid wildcard file locks. File overlap is handled by separate worktrees and integration.

Rules:

1. Acquire every resource required for the current execution stage atomically, or hold none and wait.
2. No incremental acquisition while holding resources. A discovered requirement causes a controlled stage boundary: stop/checkpoint, confirm cleanup, release, and requeue with a complete resource set.
3. Dependencies must form a DAG. Reject cycles within the same transaction used to edit edges; freeze running task inputs and dependency definitions.
4. A running task cannot synchronously wait for a child task while retaining its execution slot. MVP disallows runtime nested spawning through the coordination API; later yielding support must release the parent's resources first.
5. Release execution resources after confirmed process completion. Integration uses a separate resource stage.
6. A conflict or approval wait releases the integration writer after preserving candidate artifacts.

Proof sketch: queued tasks hold no managed resources, and active holders cannot request additional managed resources. Therefore the managed allocation protocol cannot form a hold-and-wait cycle. Dependency cycles are separately rejected. This proves absence of those cycles, not eventual task success or absence of external hangs.

Later incremental locking would require a global canonical acquisition order across every participating service. Do not mix protocols casually. Consistent lock ordering is a standard prevention technique: [PostgreSQL explicit-locking documentation](https://www.postgresql.org/docs/17/explicit-locking.html#LOCKING-DEADLOCKS).

## 8. Leases, fencing, cancellation, and crash recovery

Every attempt gets a monotonically increasing generation and current scheduler epoch. Heartbeats use a runtime-owned monitor, not an agent's self-report. Renew only if the same owner/generation remains current and the process is still observed; health renewal does not extend the execution deadline.

Controlled mutation endpoints validate attempt ID, epoch, generation, current lease, authority, and expected task revision in their transaction. Result acceptance and integration admission reject stale generations. Resource adapters must check their own lease generation before applying controlled effects.

Lease expiration sequence:

1. Mark the attempt lost/uncertain; revoke acceptance of new results.
2. Request process-tree termination and independently confirm the result.
3. Quarantine mutable resources when termination cannot be confirmed.
4. Release only resources known to be safe; retain original worktree/evidence.
5. Requeue an eligible retry with a new attempt ID, generation, and workspace.

A generation counter does not fence raw filesystem writes. Never reuse a lost attempt's workspace. Do not reassign an external mutable resource merely because its timestamp expired.

Startup reconciliation:

- Enforce a single active scheduler with an OS-level ownership mechanism plus durable epoch. A database lease alone must not permit two still-running schedulers to launch processes during takeover.
- Examine every unfinished dispatch intent, active attempt, and integration intent.
- Match a recorded process by verifiable creation identity/ownership, not PID alone, because PIDs can be reused.
- Treat the crash window between process creation and PID persistence as uncertain. Use job ownership and startup termination/reconciliation; never blindly replay a launch intent.
- If an old backend cannot be confirmed stopped, refuse takeover of its mutable resources and expose a recovery action.
- Invalidate prior epoch submissions; restore queued work only after reconciliation.

Cancellation is durable and asynchronous: record `CANCEL_REQUESTED`, request tree termination outside the transaction, confirm termination, release resources, then finalize cancellation. If termination fails, report `BLOCKED`/uncertain rather than a false completed cancellation.

Use monotonic time for live deadlines and persisted UTC for recovery metadata. Inject clocks for tests; handle wall-clock jumps conservatively and use epoch invalidation at restart.

## 9. Workspace lifecycle and overlap handling

Resolve the repository's canonical Git common directory to identify resources across multiple worktree paths. Manage workspaces beneath a Sentinel-owned root using generated IDs. Normalize Windows casing and resolve junctions/reparse points before containment checks; reject escaping paths and never trust an agent-provided cleanup path.

Git lifecycle:

1. Resolve an explicit committed base SHA. Do not silently incorporate, stash, reset, or overwrite user uncommitted changes.
2. Create a unique branch and worktree for the attempt with argument-array subprocess calls.
3. Launch the agent with that worktree as its working directory and existing environment/credential restrictions.
4. After confirmed process exit, inspect tracked changes, untracked files, renames, binaries, and declared scope violations.
5. Produce a bounded, immutable result commit/artifact only after checking which files may be included. Do not blindly stage every generated or sensitive file.
6. Retain the workspace until its result, logs, and recovery metadata are durable and retention policy permits cleanup.

Git worktrees support separate working directories while sharing repository metadata: [Git worktree documentation](https://git-scm.com/docs/git-worktree). Restrict coordination operations to managed refs and serialize Git administrative operations as needed. Agents with ambient repository access remain outside a strong isolation guarantee.

Overlap policy:

| Overlap | Initial behavior |
|---|---|
| Different files | Concurrent execution; still verify combined candidate |
| Same file, different hunks | Concurrent execution; merge and run required checks |
| Conflicting hunks | Preserve outputs; create structured conflict; no automatic acceptance |
| Shared interface | Dependency plus pinned interface artifact before consumers dispatch |
| Shared lockfile or migration order | Prefer an explicit owner/dependency; verify combined dependency/schema state |
| Shared mutable service | Isolated instance or full-stage exclusive reservation |

Path overlap is a scheduling hint. Mergeability is not behavioral compatibility. Defer AST-based conflict prediction until the basic integration gate is reliable.

## 10. Integration and verification protocol

Maintain a dedicated Sentinel integration ref per Change and a repository-wide single integration writer. Keep the user's current branch/worktree unchanged.

1. Accept an immutable result only from a valid current attempt, or from a separately authorized explicit salvage operation after review. No silent salvage of expired results.
2. Record integration intent with expected target SHA and source artifact digest.
3. Build a candidate in a fresh managed integration worktree. Handle merge conflicts as a durable blocked result.
4. Run required checks against the candidate SHA and record command, tool identity, environment evidence, outputs, exit code, and coverage limitations.
5. Verify checks did not mutate the candidate's relevant files; a changed candidate requires fresh capture and verification.
6. Revalidate authority, integration generation, and current target SHA.
7. Advance only the dedicated integration ref using Git compare-and-swap against the expected old SHA. If it moved, rebuild/reverify; do not reuse an earlier pass.
8. Persist integration completion and its journal event in one database transaction; unblock dependents using the accepted SHA.

Git ref updates and SQLite commits cannot be one atomic transaction. Persist intent before the Git operation and reconcile on restart: old target means pending/not applied; exact candidate target means applied but possibly not finalized; an unexpected target means conflict/uncertain and requires inspection. Never describe these two systems as atomically committed together.

A valid but empty result needs an explicit policy: read-only tasks can produce artifacts; implementation tasks with no effective change should report no-change, with success only if their acceptance criteria permit it.

Resolution work runs in another isolated attempt, referencing both conflicting artifacts and the current target. It may not rewrite either source result. A failed check retains diagnostics and leaves the target ref unchanged.

## 11. API and authorization contract

These are proposed route suffixes; follow the repository's existing prefix and envelope conventions.

| Method and route | Purpose |
|---|---|
| `POST /changes/{change_id}/tasks` | Create a draft task |
| `GET /changes/{change_id}/tasks` | List tasks with bounded pagination |
| `GET /changes/{change_id}/tasks/{task_id}` | Task detail, waiting reason, artifact references |
| `PATCH /changes/{change_id}/tasks/{task_id}` | Edit allowed fields with expected revision |
| `PUT /changes/{change_id}/tasks/{task_id}/dependencies` | Replace edges transactionally and reject cycles |
| `POST /changes/{change_id}/tasks/{task_id}/submit` | Validate and enqueue |
| `POST /changes/{change_id}/tasks/{task_id}/cancel` | Request cancellation |
| `POST /changes/{change_id}/tasks/{task_id}/retry` | Authorized retry with bounded policy |
| `GET /changes/{change_id}/tasks/{task_id}/attempts` | Attempt history and linked AgentRuns |
| `GET /changes/{change_id}/coordination` | Queue, capacity, integration, recovery status |
| `GET /changes/{change_id}/integrations` | Candidate status and checks |
| `POST /changes/{change_id}/integrations/{id}/resolve` | Submit a resolution task referencing preserved artifacts |
| `POST /changes/{change_id}/coordination/pause` | Stop new dispatches; existing work continues |
| `POST /changes/{change_id}/coordination/resume` | Resume new dispatches |

Scheduler internals such as lease renewal and completion can remain in-process service calls in the first release. If exposed later, use attempt-scoped credentials and the same fencing checks; never hand agents broad scheduler authority.

Add exact policy scopes for task management, scheduling, cancellation, retry, and integration, with Change boundaries. Scheduling authority must not imply arbitrary launch or credential authority. Recheck delegated launch scope and expiry immediately before dispatch and integration scope immediately before ref mutation. Task declarations can narrow existing authority, never expand it.

Use stable error codes such as `TASK_DEPENDENCY_CYCLE`, `TASK_REVISION_CONFLICT`, `RESOURCE_REQUEST_IMPOSSIBLE`, `ATTEMPT_STALE`, `PROCESS_TERMINATION_UNCONFIRMED`, `INTEGRATION_BASE_MOVED`, and `INTEGRATION_CHECK_FAILED`; map them to existing error envelopes.

Return 202 for asynchronous accepted commands where consistent with current conventions. Preserve old AgentRun API behavior and IDs. Version additive passport/event payloads; existing enum consumers and timeline renderers need compatibility tests.

## 12. User interfaces

Desktop task view:

- Task list with state, dependencies, assigned adapter, attempt count, elapsed time, and clear waiting reason.
- Capacity summary: active slots, queued tasks, verification, integration.
- Task detail: instructions, scopes, pinned base, run output, artifacts, check results, event history.
- Dependency graph with a list alternative for accessibility.
- Conflict view comparing source results and the current integration target; action to create a resolution task.
- Distinct labels for pausing dispatch and suspending an existing process. Neither implies safe resource release.
- Recovery view for uncertain attempts and quarantined resources.

Add typed service functions under `apps/desktop/src/services/` and generated schemas through the existing API generator. Follow current API transport, Electron token ownership, error handling, and styling conventions.

CLI commands should cover create/list/show, dependencies, submit, cancel, retry, queue pause/resume, attempts, and integrations. Support JSON output for automation. Add TUI list/detail and submit/cancel/retry/queue controls using the same API; do not create a separate scheduling implementation in a client.

## 13. Evidence and passport

Journal events should cover task creation/edit/submission, dependency changes, resource claims, dispatch intent, observed run association, attempt completion/loss/cancellation, retry, stale-result rejection, conflict, verification, integration intent/completion, and resource quarantine/release.

Every event carries applicable Change/task/attempt/run IDs, scheduler epoch, generation, actor, reason, and immutable artifact references. Redact secrets and bound payloads; store large artifacts separately by digest. Avoid full instructions or raw model output in every event.

Passport extension records the graph digest, accepted result commits, rejected attempts, evidence limitations, source-to-candidate relationship, verified target SHA, and required checks. Replay must validate new event schemas without breaking old journals. A signature proves integrity under the existing signing model; it does not prove agent correctness.

Expose counts/durations: accepted tasks per minute, queue wait, execution time, verification time, conflict rate, retries, stale rejections, uncertain attempts, and crash recovery time. Define acceptance as a successful integration or validated read-only artifact, not process exit.

## 14. Ordered implementation phases

### Phase 0: contracts and baseline

Inspect repository guidance and current tests; record baseline failures without changing unrelated code. Finalize state transitions, policy scopes, ownership protocol, and minimal API schemas. Write a concise decision log in `docs/MULTI_AGENT_BUILD_STATUS.md`.

Exit: model/state/API design covers cancellation, duplicate requests, crash uncertainty, and existing-client compatibility. No implementation claim based only on README counts.

### Phase 1: durable tasks and dependency graph

Implement additive migrations, repositories, revision checks, idempotency, task management service, cycle validation, and atomic journal writes. Add task CRUD/dependency/submit API and CLI commands; initially keep actual dispatch disabled.

Tests: migration from existing database; idempotency replay/conflict; stale revision rejection; concurrent cycle-creating edge edits; cross-Change access denial; domain/journal rollback.

Exit: tasks and graph survive restart, and no queued task launches before scheduler support is enabled.

### Phase 2: isolated workspaces and immutable results

Implement managed worktree lifecycle with canonical repository identity, safe paths, base pinning, artifact capture, and explicit retention states. Integrate with existing Git/evidence conventions.

Tests: paths with spaces; Windows aliases/junction escape; dirty user worktree preservation; rename/binary/untracked handling; scope violation; cleanup refusal for foreign/active workspaces.

Exit: two attempts can edit the same relative file without changing each other's working file or the user's current branch.

### Phase 3: scheduler and supervised dispatch

Implement capacity and atomic resource claims, durable attempts/dispatch intents, bounded worker pool, owner epoch, startup/shutdown hooks, and explicit AgentRun linkage. Recheck launch authority at execution time.

Tests: two claimants racing for one task; two slots with three tasks; all-or-none acquisition; impossible requests; bounded database lock contention; API remains responsive while a fake agent blocks; callback failure does not falsely finalize a task.

Exit: durable queue runs independent tasks concurrently with no duplicate claim and deterministic waiting reasons.

### Phase 4: recovery, fencing, and cancellation

Implement heartbeats, deadlines, stale-result rejection, process termination confirmation, quarantine, retries, and startup reconciliation. Inject crashes around intent creation, process launch, run association, completion, and lease release.

Tests: expired lease while old worker runs; duplicate completion; lost heartbeat; PID reuse; scheduler restart; concurrent takeover; stop failure; cancellation during launch; clock jumps; authority revoked while queued.

Exit: no stale result is accepted, no uncertain shared resource is reused, and every unresolved process is visible as uncertain rather than successful.

### Phase 5: serialized integration and verification

Implement candidate creation, merge/conflict reporting, pinned checks, compare-and-swap ref advance, integration intents/reconciliation, and resolution tasks. Use the existing assurance engine without inventing missing coverage.

Tests: clean merge; textual conflict; semantic incompatibility caught by integration tests; target moves after checks; check mutates candidate; crash before/after ref update; duplicate integration request; dependency consumes correct accepted base.

Exit: the accepted ref always points to the exact checked candidate; failed/conflicting candidates never advance it.

### Phase 6: operator surfaces and passports

Complete CLI, desktop, and TUI flows; regenerate OpenAPI and TypeScript types; add coordination provenance to replay/passport with compatibility tests. Display blocked and uncertain states with actionable reasons.

Tests: API snapshot/type parity; user can create three tasks, set edges, submit, cancel/retry, resolve conflict, inspect evidence, and export passport; old AgentRun routes still function; renderer never receives backend secrets.

Exit: the operator can complete the core workflow from the desktop without direct database edits or fabricated status.

### Phase 7: demonstration and failure validation

Provide repeatable fake agents and a temporary sample repository. Run the two-slot/three-task demo, conflict resolution, stale result, crash recovery, and external target-movement scenarios. Measure throughput and recovery against a sequential baseline on identical fixtures.

Exit: record real commands/results, platform skips, artifact paths, and remaining limitations. Update README to describe only verified capabilities.

## 15. Verification strategy and commands

Use deterministic fake workers and injected clocks for scheduler/resource tests. Use real temporary Git repositories for worktree/integration behavior. Use real Windows supervision tests for process-tree claims; mocks alone cannot establish that guarantee. Synchronize races using barriers/events rather than fragile sleep timing.

Suggested new suites: `backend/tests/coordination/test_tasks.py`, `test_scheduler.py`, `test_resources.py`, `test_workspaces.py`, `test_reconciliation.py`, `test_integration.py`, and coordination acceptance tests. Add model-based/randomized sequences with fixed seeds to check capacity, unique ownership, legal transitions, and no stale acceptance across many interleavings.

Existing project commands, to run when relevant dependencies are installed:

```powershell
# Repository root: start with the focused suite created by the implementation.
python -m pytest backend/tests/coordination
python -m pytest backend/tests/acceptance backend/tests/core backend/tests/execution backend/tests/git
python -m pytest

# Desktop contract/client/UI changes.
Set-Location apps/desktop
npm run api:generate
npm run api:check
npm run typecheck
npm test
npm run build
npm run test:e2e
```

Update `openapi.json` from the backend using the repository's actual snapshot procedure before `api:generate`; the generator reads the snapshot, not the running backend. Inspect current contract tests to determine exact regeneration conventions. Register new frontend unit tests in the explicit `test:unit` file list if applicable. E2E tests require their configured environment; report missing dependencies honestly.

Do not run the entire suite after every edit. Run focused tests per phase and complete the full relevant regression pass before calling the feature complete.

## 16. Release acceptance checklist

- [ ] Existing individual-agent workflows remain compatible.
- [ ] Three tasks run with at most two execution attempts active.
- [ ] Concurrent claims never exceed capacity or create two live attempts for one task.
- [ ] Dependencies reject cycles and use pinned accepted inputs.
- [ ] Overlapping edits remain independently recoverable.
- [ ] Managed resource requests never hold a partial set while waiting.
- [ ] Stale generations and prior scheduler epochs cannot submit accepted results.
- [ ] Unknown process termination quarantines resources instead of silently reusing them.
- [ ] Crash windows are reconciled without blindly relaunching uncertain intents.
- [ ] Combined verification is tied to the exact candidate and target compare-and-swap.
- [ ] Git/database cross-system uncertainty is explicitly reconciled.
- [ ] Cancellation, retries, authority expiry, and impossible requests are visible and tested.
- [ ] Every coordination domain mutation and its journal entry share a transaction.
- [ ] Passport identifies source attempts, accepted candidate, checks, and limitations.
- [ ] Cleanup never deletes user-owned work or an active attempt workspace.
- [ ] Desktop, CLI, and TUI expose their promised controls through the same API.
- [ ] No claim of sandboxing, arbitrary exactly-once execution, or universal deadlock freedom.

## 17. Sharing backend changes and integrating the frontend

Deliver each capability as a vertical slice: backend behavior, published API contract, typed frontend service, operator interaction, and an end-to-end check. The ordered backend phases establish prerequisites; add their usable UI slices as soon as the corresponding API exists. Phase 6 completes the experience rather than being the first time a real frontend talks to the backend.

### Shared contract and ownership

The backend owns state transitions, authorization, resource accounting, and integration decisions. The frontend renders returned state and submits user intent. It must not reproduce scheduling logic or infer completion from a timer, closed output stream, or successful HTTP submission.

Use this dependency chain for every API change:

```text
Pydantic request/response models and route implementation
    -> backend contract tests and exported openapi.json
    -> generated desktop schema and exported client types
    -> typed coordination service and query keys
    -> task screens, actions, errors, and refresh behavior
    -> live backend integration test
```

One integration owner maintains shared schemas, migrations, route wiring, OpenAPI snapshot, and generated artifacts. This is a responsibility assignment, not an instruction to launch parallel agents. If separate developers work on the two sides, the frontend can build against agreed fixtures while the backend is implemented, but the slice remains incomplete until tested against the real service.

### Backend-to-frontend handoff

For each slice, add a handoff entry in `docs/MULTI_AGENT_BUILD_STATUS.md` containing:

- Slice name, implemented files, and current readiness: proposed, contract agreed, backend tested, frontend wired, or integrated and verified.
- Full route including `/api/v1`, HTTP method, operation ID, and request/response schema names.
- Required actor/Change scope, revision checks, idempotency behavior, and asynchronous completion semantics.
- Validated example request, successful response, and representative error responses with stable reason codes.
- Returned task/attempt/integration states, nullable fields, timestamp formats, pagination, and waiting reasons.
- Queries to refresh after each mutation and how the client detects terminal status.
- Migration requirements, backend startup instructions, feature availability, and compatibility limits.
- Contract snapshot digest or local commit identifier when available, focused test commands/results, and unresolved integration issues.

Generate examples from tested fixtures or actual API responses. Clearly mark proposed examples until the backend supports them. Share changes through repository files and reviewable diffs; no publishing or pushing is required by this plan.

### Concrete wiring sequence

1. Implement the backend service and route through existing policy/auth/error boundaries. Add request validation, response models, idempotency, and acceptance tests before declaring an endpoint ready.
2. Export the updated backend OpenAPI snapshot using the project's contract-test conventions. Run `npm run api:generate` from `apps/desktop`, then `npm run api:check` and `npm run typecheck`.
3. Export coordination aliases from `apps/desktop/src/lib/api/types.ts` following existing patterns. Create `apps/desktop/src/services/coordination.ts` with typed requests and query options modeled on `services/actions.ts` and `services/changes.ts`.
4. Check transport support before choosing methods. The inspected `lib/api/index.ts` exposes GET, POST, and PUT helpers; the proposed PATCH task route needs a typed PATCH helper and verification across browser/Electron transports. PUT currently does not forward an idempotency option. Extend the shared options consistently for idempotent PUT/PATCH mutations and test transport/header propagation rather than bypassing the client.
5. Add task components under a cohesive feature directory and connect them to the current Change workspace/router. Existing agent views should link to task attempts while preserving independent AgentRun controls.
6. Keep cache keys scoped by Change and task, for example `['coordination', changeId, 'tasks']` and `['coordination', changeId, 'task', taskId]`. Refresh task detail/list, attempts, capacity, integrations, timeline, and passport freshness as applicable after mutations.
7. Use bounded polling for active views in the MVP, following existing query conventions. Slow or stop polling when hidden/terminal, back off on connectivity failures, and refetch on reconnect. Do not introduce WebSockets merely to display task progress.
8. Preserve one idempotency key across network retries of the same user intent. A deliberate new retry-task action is a new intent. On revision conflict, fetch current state and explain the conflict; do not blindly overwrite newer input.
9. Render initial loading, empty queue, ready, running, waiting, blocked, cancellation pending, failed, and completed states. Show stale/disconnected data honestly. A 202 response means accepted for processing, not completed.
10. Keep backend tokens in the existing Electron main-process path. If route/method restrictions exist in the proxy, update their explicit allowlists and tests; do not weaken the boundary or put credentials in components.

### Feature integration map

| Slice | Backend delivery | Frontend delivery | End-to-end proof |
|---|---|---|---|
| Tasks and graph | CRUD, revisions, DAG validation, submit | Task form/list/detail and dependency editor | Create, reload, edit, reject cycle, submit |
| Queue and capacity | Dispatcher, slot claims, queue status | Capacity display and precise waiting reasons | Three tasks share two slots; UI reflects the queued third task |
| Attempts and control | Run association, cancellation, retries | Live attempts/output, cancel/retry actions | Cancel remains pending until confirmed; retry has a new attempt ID |
| Recovery | Lost-attempt detection and quarantine | Recovery state with affected resources and reason | Backend restart exposes uncertainty and rejects stale completion |
| Integration | Candidate, conflicts, checks, target advance | Candidate/check details and resolution action | Resolve overlapping edits and show the exact verified commit |
| Evidence | Coordination events and passport extension | Timeline, provenance, export | Export includes the accepted attempt/candidate and rejected history |

### Compatibility and completion gates

Expose coordination availability through the existing backend capability mechanism, extending it additively if necessary. A frontend connected to a backend without coordination support shows an unavailable state rather than inventing an empty working scheduler. Keep previous individual-agent flows usable and do not turn unrelated backend errors into a misleading unsupported-feature message.

Run fixture-based UI tests for edge states plus at least one real backend workflow against a temporary repository. Exercise both the browser development transport and Electron proxy boundary where relevant. Mock-only tests cannot establish frontend/backend integration.

A slice is complete only when its handoff entry has the implemented contract, generated types are current, the actual UI action reaches the authorized backend, persisted results survive reload, errors render correctly, and the real-service check has recorded evidence. Keep all changes local unless separately authorized to push.

## 18. Optional next milestones

After the checklist passes, consider checkpointable adapter sessions, capacity partitioning across Changes, fair reservation for large jobs, provider-aware rate limiting, semantic overlap hints, and isolated remote workers. Each requires its own measurable acceptance criteria and threat-boundary review. Do not let these delay the reliable local vertical slice.
