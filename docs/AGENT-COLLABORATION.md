# OdontoFlow — Development and Agent Collaboration

This is the cross-repository playbook for humans and coding agents working on
OdontoFlow/AIRYTHM. It explains how a request becomes a safe, testable change.
The planning repository remains the project control plane; this document is
the backend-facing guide to the same workflow.

## Read order

Before writing code, read these in order:

1. The planning repository's `orchestration/current-activity.yaml` and
   `STATUS.md`. They decide what is authorized next.
2. This repository's `AGENTS.md`. It defines the backend invariants and
   protected surfaces.
3. This repository's `README.md`, `DEVELOPMENT.md`, and the relevant curated
   document under `docs/`.
4. The approved specification or living brief for the activity.
5. The latest handoff and evidence for the affected boundary.

If the current activity is `OWNER-DECISION-REQUIRED`, do not select an
implementation task by assumption. Record the owner's choice in the planning
control plane first.

## Product boundary

Reception/Scheduling v1 is the business path:

```text
lead/patient
  → conversation/message
  → service
  → location
  → practitioner
  → deterministic availability
  → appointment proposal
  → authorized confirmation
  → appointment
```

PostgreSQL and deterministic application services are the authority for this
path. AIRY may reason, ask questions, and select typed tools; it cannot set a
price, invent a slot, bypass a permission, or write directly to PostgreSQL.
Booking remains fail-closed until an authorized confirmation is present.

## Repository boundaries

| Repository | Owns | Must not become |
|---|---|---|
| `odontoflow-planning` | status, decisions, activity authorization, plans, CAVELOG, project handoffs | a product-code repository |
| `odontoflow-backend` | FastAPI contracts, PostgreSQL schema, IAM, scheduling, deterministic services, audit, typed agent gateway | a channel-specific business workflow or an LLM authority |
| `odontoflow-frontend` | ERP presentation and user interaction against the backend contract | a second scheduling or pricing engine, or a direct database client |
| `odontoflow-voice` | speech/transcription and structured drafts | a booking writer or source of canonical clinic state |
| `odontoflow-sim` | synthetic clinic scenarios and ground truth for experiments | real clinic data or canonical production state |
| n8n/channels | transport and orchestration at the API boundary | business rules, appointment writes, or a parallel database |

The legacy `MediStock` repository is read-only reference material. It is never
an implementation target or a writable collaboration surface.

## Development logic: one bounded vertical slice

Every product activity follows the same loop. The activity is complete only
when its evidence is written down and its definition of done is satisfied.

1. **Translate the requirement.** Write the business outcome, acceptance
   criteria, explicit non-goals, dependencies, allowed files, and required
   evidence in one living brief.
2. **Inspect reality.** Verify the fresh Git state, current runtime, schema,
   route, and existing tests. Classify each relevant fact as
   `REAL`, `PARTIAL`, `SIMULATED`, `MISSING`, or `UNVERIFIED`.
3. **Choose the boundary.** Put business rules in backend domain services;
   put presentation in frontend; put speech/channel mechanics in adapters;
   put project decisions in planning. Prefer an existing contract over a new
   framework or side channel.
4. **Write the proof first.** Add the smallest failing deterministic test for
   the invariant. Backend persistence and concurrency tests use real
   PostgreSQL; no SQLite substitute is accepted for database behavior.
5. **Implement the smallest slice.** Keep routers thin, pass explicit
   `ExecutionContext`, enforce permissions and idempotency in the service,
   and stage audit in the same transaction as the mutation.
6. **Verify in layers.** Run focused tests first. If application behavior or
   a contract changed, run the complete PostgreSQL suite once, serially, after
   all edits. Never run two suites against the shared test database.
7. **Review independently.** A read-only reviewer checks the diff against the
   brief, protected surfaces, tenant isolation, idempotency, failure truth,
   and the recorded test output. Corrections return to the same writer.
8. **Commit and hand off.** Make one scoped commit using
   `feat|fix|test|docs: <summary>`, preserve unrelated dirt, and write a
   self-contained handoff with the exact commit, evidence, blockers, and one
   recommended next activity.

The desired shape is:

```text
authorized brief
  → reality/evidence
  → failing proof
  → smallest implementation
  → focused verification
  → full verification when warranted
  → independent review
  → one commit
  → durable handoff
```

## Contract ownership

| Concern | Authoritative surface | Acceptance proof |
|---|---|---|
| tenant identity and permissions | backend IAM + `ExecutionContext` | unauthorized and cross-tenant requests fail closed |
| services, duration, locations, practitioners | backend catalog/organization services | typed schemas reject caller-owned authority fields |
| availability | `app/scheduling/availability.py` plus scheduling queries | deterministic timezone/grid/overlap tests |
| proposal and appointment | backend scheduling services and PostgreSQL constraints | one proposal and one authorized appointment under replay/race |
| conversation and messages | canonical backend persistence | authenticated ingress and idempotent replay evidence |
| AIRY decisions | `sales_agent/` and the typed gateway | agent can only call allowlisted, tenant-authorized tools |
| channel delivery | inbound/outbound adapters and sandbox contracts | provider isolation, durable receipt, settlement, retry/dead-letter evidence |
| UI behavior | frontend consuming the published API contract | typecheck, unit/build, and contract E2E when affected |
| project status and next work | planning `STATUS.md`, CAVELOG, current activity, handoff | exact HEADs and evidence links |

When a contract changes, update the owner first, then its consumers. Do not
make the frontend, n8n, voice service, or agent infer a new backend rule from
local code.

## Agent roles and collaboration rules

| Role | Responsibility | Write policy |
|---|---|---|
| coordinator | selects authorized scope, resolves evidence, sequences work, and owns the final status | may update the activity/handoff; does not grant itself a new product scope |
| scout | answers a narrow read-only question with file/command evidence | no product writes, no speculative plan |
| planner | converts verified evidence into a dependency-ordered plan and definition of done | planning artifacts only; no product implementation |
| writer | implements one bounded change in one assigned repository/surface | one writer per overlapping surface; tests before implementation |
| reviewer | independently checks the diff and fresh test output | read-only; any correction goes back to the same writer |

Parallel read-only inspection is useful only when it reduces uncertainty. Two
writers must never edit overlapping files, migrations, contracts, or business
rules. All agents use the same verified base commit and persist compact
artifacts rather than relying on chat transcripts.

## Change routing between repositories

Use this routing before dispatching work:

| Request | First write surface | Follow-up |
|---|---|---|
| new business rule, schema, permission, endpoint, or persistence invariant | backend | update API/integration docs and frontend only after the contract is verified |
| screen, form, table, or presentation state using an existing contract | frontend | prove it against the backend contract; no domain duplicate |
| voice parsing or speech UX | voice | emit a structured draft; confirmation and mutation remain backend-owned |
| synthetic scenario or measurement fixture | sim | mark all data synthetic; never promote it to clinic truth |
| inbound/outbound transport | integration adapter or n8n artifact | call canonical authenticated endpoints; keep business logic in backend |
| provider/model configuration | backend runtime configuration | server-owned provider/model/credentials; no caller override or automatic fallback |
| project decision, prioritization, or cross-repo status | planning | link the affected repository handoff and exact HEAD |

## Safe local workflow

From the repository being changed:

```bash
git fetch --prune
git status --short --branch
git log -8 --oneline --decorate
```

Do not reset, clean, rebase, force-push, overwrite local environment files, or
discard unrelated work. Confirm the canonical remote before publishing and use
normal fast-forward pushes only.

For the backend:

```bash
docker start odontoflow-db-1
uv sync --locked
uv run alembic upgrade head
uv run python -m pytest -q
uv run python -m app.run
```

Use the focused test command for the affected module before the full suite.
Never print environment values, bearer tokens, provider request bodies, patient
messages, or database passwords. A preflight may report only booleans and
sanitized names. Do not make a paid provider request unless the current
activity explicitly authorizes exactly that smoke.

## Verification standard

Evidence must distinguish these states:

- `REAL`: observed through the canonical running path and durable state.
- `PARTIAL`: one boundary is proven but the complete flow is not.
- `SIMULATED`: fake model, sandbox provider, or synthetic data only.
- `MISSING`: the capability is not present.
- `UNVERIFIED`: the code may exist, but the required proof was not run.

For Reception/Scheduling, a complete acceptance record answers all of these:

1. Can an authenticated event enter once and replay without duplication?
2. Can AIRY read state only through authorized typed tools?
3. Is availability deterministic from canonical clinic state?
4. Can a proposal be created exactly once?
5. Can an authorized confirmation create exactly one appointment?
6. Do timeouts and failures preserve truthful partial state?
7. Are automatic appointments zero when confirmation is absent?
8. Can channels and n8n be replaced without rewriting scheduling services?
9. Are provider credentials and model selection server-owned?

An HTTP `200`, a model response, or a passing unit test alone does not prove
the business flow. Record database IDs/counts, tool audit evidence, receipts,
and side-effect counts when the activity requires them.

## Handoff contract

Every completed or blocked activity leaves one Markdown handoff with:

```markdown
# <ACTIVITY> — handoff

## Status
PASS | PARTIAL | BLOCKED | NOT DONE

## Scope and contract
- business outcome
- explicit non-goals
- definition of done

## Verified reality
- base HEAD and repository state
- changed files or `none`
- evidence paths and commands

## Tests and runtime proof
- focused result
- full-suite result, or why it was not warranted
- database/HTTP/integration evidence

## Safety and side effects
- tenant/provider isolation
- idempotency/replay behavior
- appointments/outbound/receipts created
- secrets and sensitive payloads absent from evidence

## Blockers and next single activity
- exact blocker or limitation
- one recommended next activity
```

The planning repository indexes the handoff and records a changed project
status. The handoff is the durable truth; chat is not.

## Current verified boundary

At the last published verification, the deterministic backend core, typed AIRY
gateway, authenticated canonical sandbox ingress, and sandbox outbound receipt
path were present. Reception/Scheduling v1 was still open as a component, the
real-model complete-loop proof was not closed, and the planning control plane
required an explicit owner decision before another implementation activity.
Consult the current planning snapshot before acting; this paragraph is a
navigation aid, not permission to start work.

- Project control plane: `https://github.com/MiguelAAR10/OdontoFlow-Planning`
- Current status: `../../odontoflow-planning/STATUS.md` locally, or the planning
  repository's `STATUS.md` on GitHub
- Current activity: `../../odontoflow-planning/orchestration/current-activity.yaml`
- Backend architecture: [`architecture.md`](architecture.md)
- Backend engineering contract: [`../AGENTS.md`](../AGENTS.md)

## Completion gate

Declare an activity complete only when its contract is observable, its
protected surfaces are intact, its tests are fresh, its diff is scoped, and a
handoff points to the evidence. If a result is only a sandbox, fake-model, or
synthetic proof, say so explicitly and leave the live integration fail-closed.
