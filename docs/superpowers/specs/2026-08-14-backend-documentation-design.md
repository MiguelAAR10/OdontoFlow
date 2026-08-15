# Backend Documentation Design

## Goal

Make OdontoFlow understandable and safe to evolve for a senior backend engineer, a new contributor, or an AI agent without requiring them to reverse-engineer the implementation first.

## Scope

This documentation release adds or updates:

- `AGENTS.md`: engineering contract for all agents and contributors.
- `CLAUDE.md`: concise execution guide for Claude-compatible agents.
- `CHANGELOG.md`: traceable delivery history.
- `docs/architecture/backend.md`: current backend architecture, module boundaries, data flow, and invariant ownership.
- `docs/quality-and-testing.md`: test strategy, commands, database requirements, and validation criteria.
- `docs/MIGRATION.md`: legacy MediStock migration position, including consumables, inventory, and sales.
- `docs/api/openapi.yaml` and `docs/api/openapi.json`: generated FastAPI contract snapshots.
- `README.md`: links to the canonical documents and an explicit migration/status summary.

No application, schema, migration, endpoint, or business behavior changes in this release.

## Information Architecture

`README.md` remains the entry point: product summary, quick start, endpoint index, and links.

| Document | Question answered |
|---|---|
| `AGENTS.md` | How must work be performed in this repository? |
| `CLAUDE.md` | What execution protocol does an AI coding agent follow? |
| `docs/architecture/backend.md` | How does the implemented backend work today? |
| `docs/quality-and-testing.md` | How is correctness established and reproduced? |
| `docs/MIGRATION.md` | What is and is not migrated from MediStock, and in what order? |
| `docs/api/openapi.yaml` | What exact HTTP contract is implemented? |
| `CHANGELOG.md` | What changed in each shipped iteration? |

## Architecture Content Requirements

The backend architecture document describes the implemented appointment lifecycle flow:

`FastAPI router -> request schema -> explicit ExecutionContext -> application service -> PostgreSQL -> AuditEvent`.

It names implemented modules (`commercial`, `catalog`, `organization`, `scheduling`, `iam`, `audit`) and their responsibilities, as well as database-owned invariants: organization composite foreign keys, service-authoritative duration, and practitioner-global partial GiST appointment exclusion.

It must distinguish implemented behavior from intended platform behavior. The document must surface these verified gaps rather than describing PF2/PF3 as globally complete:

- HTTP currently resolves every request as the seeded `system` principal in the bootstrap organization; this is a development compatibility boundary, not production authentication.
- `create_organization` does not yet call `provision_system_access`, so a new organization is not atomically provisioned with the required system membership and role assignment.
- Explicit `ExecutionContext` and permission enforcement are currently wired to appointment booking, cancellation, and rescheduling; the remaining tenant-scoped reads and writes retain their pre-PF3 transport path.
- Appointment services retain a compatibility path where a direct caller can omit `ctx`; that path resolves a default context and does not execute the explicit permission guard. It is test compatibility, not an authorization boundary.

The release reports these as platform-hardening work before Clinical Bridge. It does not change application behavior.

## Quality Content Requirements

The quality document must provide reproducible validation:

- Tests use real PostgreSQL, never SQLite, on host port 5434.
- Full suite: `.venv/bin/python -m pytest -q`.
- Tests must not run concurrently because they share `odontoflow_test`.
- Concurrency proof uses distinct sessions, threads, and deterministic synchronization; no sleeps.
- Completion requires focused verification, full suite, clean allowed diff, and generated OpenAPI parse validation.

## Migration Position

MediStock remains read-only reference code. The current scheduling vertical deliberately excludes patient clinical care, stock, consumptions, sales, invoicing, analytics, and external integrations.

Consumables/inventory/sales are not migrated. The intended dependency order is:

1. Clinical Bridge: `Patient`, `Visit`, `ServiceExecution`.
2. Inventory/Operations: `Product`, immutable `StockMovement`, `StockBalance`, `Consumption` tied to a service execution.
3. Finance: `Charge`, `Payment`, invoice/export adapters.

The new design must never recreate legacy direct mutable-stock behavior; inventory is ledger based, and clinical consumption is tied to a real execution.

## Changelog Content Requirements

`CHANGELOG.md` uses Keep a Changelog sections and records commits grouped by milestone: bootstrap, Vertical 1, PF0, PF1, PF2, PF3, and this documentation release. Each entry identifies what changed and why it matters.

## Validation

Before commit:

1. Regenerate OpenAPI from `app:app` and validate both YAML and JSON parse.
2. Confirm API snapshot reports 14 paths and 22 schemas.
3. Run the full test suite once.
4. Verify repository links, document links, and no legacy code has changed.
5. Record the exact generation command so API snapshot drift is reproducible.

## Out of Scope

- Implementing Clinical Bridge, inventory, consumables, sales, finance, or external adapters.
- Changing authentication, authorization behavior, database schema, or API behavior.
- Editing `../medistock`.
