# Claude build prompt for Sentinel multi-agent coordination

Open Claude Code in this repository and paste the prompt below. The implementation plan is a proposal; the prompt asks Claude to inspect the actual code and build it in ordered phases.

```text
You are implementing multi-agent coordination in the existing Sentinel repository.

Read docs/MULTI_AGENT_IMPLEMENTATION_PLAN.md fully. Treat its first-release scope,
invariants, ordered phases, and acceptance checklist as the implementation contract.
This is an implementation request: build the feature, run meaningful checks, and
record evidence. Do not stop at suggesting an architecture or producing scaffolding.

FIRST ACTIONS
1. Read applicable AGENTS.md/CLAUDE.md and repository guidance. Inspect git status
   and preserve existing user changes. Do not reset, clean, or overwrite unrelated work.
2. Inspect the existing launcher, supervision, policy, database, journal, migrations,
   Git adapters, verification, contracts, composition root, API clients, and tests.
3. Check the plan against actual code. Correct routine assumptions in a short decision
   log; surface a material incompatible requirement before implementing dependent work.
4. Create docs/MULTI_AGENT_BUILD_STATUS.md with phase status, decisions, changed files,
   tests and actual results, unresolved limitations, and the next concrete action.
5. Establish a relevant test baseline, then implement Phase 1. Continue in phase order
   through the release checklist, updating status at each phase boundary.

IMPLEMENTATION RULES
- Extend the existing architecture. Keep FastAPI, SQLite WAL, current policy and
  credential boundaries, Windows supervision, journal, and existing clients.
- Keep new coordination services cohesive under backend/app/coordination. Additive
  public schemas belong in the project's established contract structure.
- Maintain distinct Task, Attempt, and AgentRun identities. Persist dispatch intent
  before launch. Do not use in-memory callbacks as the correctness boundary.
- Keep SQLite transactions short. Claim resources, transition domain state, and write
  the corresponding journal event in one transaction. No process launch, Git command,
  check execution, network request, or human wait inside that transaction.
- Acquire all resources for a stage atomically or none. Reject cyclic dependencies
  and impossible requests. Never let a parent hold a slot while awaiting child work.
- Multiplex queued tasks over a bounded execution pool using fresh invocations first.
  Do not pretend arbitrary model sessions can be checkpointed or resumed.
- Use isolated managed worktrees per attempt, immutable base/result commits, and
  canonical repository identity. Preserve the user's active branch and dirty files.
- Implement scheduler ownership, epochs, fencing generations, deadlines, heartbeat
  monitoring, termination confirmation, quarantine, and startup reconciliation.
- Expired leases do not prove a worker stopped. Do not reuse uncertain mutable
  resources. Do not blindly replay a launch intent after an ambiguous crash.
- Fencing only protects operations passing through a validating boundary; worktrees
  and restricted tokens are not a filesystem/network sandbox.
- Integrate through one writer per repository into a dedicated Sentinel ref. Verify
  the exact candidate; advance using expected-old-SHA compare-and-swap. Reconcile
  the crash window between Git ref mutation and SQLite finalization explicitly.
- Validate authority at API entry and again at deferred execution/integration.
  Task scopes may narrow existing authority but cannot grant new privileges.
- Preserve old API operations and clients. Regenerate the backend OpenAPI snapshot
  and generated desktop types using actual repository conventions. Never hand-edit
  generated types as a substitute for updating their source.
- Add operator controls, clear waiting/recovery reasons, and passport provenance.
  Do not expose implementation internals in ordinary UI copy unless actionable.
- Do not add external infrastructure, remote workers, speculative semantic merging,
  production deployment, or new paid services to the first-release scope.
- Do not push, publish, or perform unrelated destructive operations. Necessary local
  implementation and tests are authorized. Keep changes reviewable by phase.

BACKEND AND FRONTEND INTEGRATION
Follow Section 17 of the implementation plan. Build usable vertical slices as backend
APIs become available; do not leave the frontend disconnected until the last phase.
For every slice:
1. Implement and test backend models, policy checks, routes, state transitions, and
   errors. Treat the backend as the authority for state and scheduling decisions.
2. Record a backend-to-frontend handoff in docs/MULTI_AGENT_BUILD_STATUS.md: full route,
   method, schema names, tested payload examples, scopes, revisions, idempotency,
   async states, error codes, refresh rules, migration/setup needs, and test evidence.
3. Update openapi.json from the actual backend and regenerate desktop types. Add
   typed coordination services and query keys using the existing client conventions.
4. Inspect method support across shared helpers and browser/Electron transports.
   The proposed PATCH endpoint needs client support; PUT/PATCH mutations need
   idempotency propagation where required. Test these boundaries instead of issuing
   raw fetch calls from components or weakening Electron proxy restrictions.
5. Wire real task forms, dependencies, queue/capacity, attempt controls, recovery,
   conflict resolution, checks, and passport views to these typed services.
6. Implement bounded polling, scoped cache invalidation, pending/loading/error states,
   reconnect handling, and revision-conflict recovery. Reuse the idempotency key for
   a network retry of the same intent. Never display HTTP 202 as completed work.
7. Keep secrets in the current backend/Electron ownership path. Detect unsupported
   coordination capability without breaking existing individual-agent workflows.
8. Test each usable slice against a real local backend and temporary repository.
   Frontend mock tests alone do not prove integration. Record that reload restores
   persisted state and that backend errors reach the UI with useful explanations.

Use repository handoff entries and reviewable diffs to share backend changes with
frontend work. Keep one owner for shared contracts and generated artifacts if parallel
work is later authorized. Do not push or publish changes. A feature is incomplete
until its backend, frontend, contract, and real-service verification agree.

EXECUTION STYLE
Work autonomously through routine implementation decisions and reversible fixes.
Ask only for missing information that materially blocks correct or authorized work;
continue independent work while it is pending. Do not repeatedly ask to continue.
Keep progress updates concise: what works, what evidence supports it, what is next.
Do not launch subagents by default. If I explicitly authorize parallel agents later,
give them bounded file ownership and preserve each other's edits; one integrator
owns shared contracts, migrations, composition wiring, and API snapshots.

TEST REQUIREMENTS
Use deterministic fake workers and injected clocks for scheduling tests, real
temporary Git repositories for integration tests, and Windows process-tree tests
for supervision claims. Exercise real interleavings using barriers/events.

Required scenarios include:
- Two claimants race for one task; three tasks share two slots.
- All-or-none resource acquisition and rejection of impossible requests.
- Concurrent graph updates cannot create a dependency cycle.
- Overlapping edits remain separate; conflict resolution preserves both artifacts.
- A stale worker cannot submit an accepted result after replacement.
- Unknown termination quarantines resources; PID reuse is not mistaken for ownership.
- Crashes before/after launch and before/after Git ref mutation reconcile honestly.
- Duplicate requests/completions do not cause duplicate accepted effects.
- Cancellation during launch and expired/revoked authority are handled correctly.
- Combined checks catch incompatible changes and never validate a different SHA.
- Domain mutation and journal append roll back together on injected failure.
- Existing AgentRun workflows, journal replay, passports, and clients remain compatible.

Run focused checks per phase. Before declaring completion, run the relevant backend
regression suite, desktop API/type checks and build, and the promised UI flows.
Record commands, actual outcomes, platform skips, and environment blockers. Never
report tests as passed if they were not run. Fix regressions introduced by this work.

CONTEXT AND COMPLETION
If context gets low, update docs/MULTI_AGENT_BUILD_STATUS.md with exact continuation
instructions before compacting. Resume from existing work, not from the beginning.
Do not label partial implementation complete. If genuinely blocked, document the
specific blocker and finish unaffected work.

At completion, summarize delivered behavior, key files, test evidence, repeatable
demo instructions, and remaining limitations. Update the plan checklist and README
only to reflect demonstrated capabilities. Leave a coherent, reviewable repository.
```

## Resume prompt

Use this in a fresh Claude session if the build spans multiple sessions:

```text
Continue implementing Sentinel multi-agent coordination. Read applicable repository
guidance, docs/MULTI_AGENT_IMPLEMENTATION_PLAN.md,
docs/CLAUDE_MULTI_AGENT_BUILD_PROMPT.md, and docs/MULTI_AGENT_BUILD_STATUS.md.
Inspect git status and the existing changes. Preserve completed work and user edits.
Resume at the next incomplete phase under the build prompt's rules. Verify the
recorded state with focused checks when needed; do not restart the implementation
or repeat completed research. Continue until the release checklist is satisfied or
a concrete blocker prevents further progress. Record actual evidence and limitations.
```
