# FE3A Service-to-Cash V1 backend handoff

Date: 2026-09-06
Authority: `odontoflow-planning/docs/plans/2026-09-06-service-to-cash-v1.md`
Coordinator freeze: D1=yes, D2=yes

## Scope shipped

- BE-1 charge context projection and derived status/date/context filters.
- BE-2 tenant-scoped `GET /executions` with charge and uncharged filters.
- BE-3 patient projection on appointment reads.
- BE-4 partial appointment-to-visit uniqueness guard and stable duplicate error.
- BE-5 typed payment codes, reconciliation metadata, historical digital-row
  preservation, organization/method/reference uniqueness, payment worklist,
  one-way verification, and `payments.manage`.
- BE-7 charge follow-ups with clinic-local promised dates, active-debt
  filtering, idempotent commands, and atomic settlement closure from payment.
- BE-8 mechanically regenerated OpenAPI JSON/YAML.

No refunds, credits, fees, write-offs, accounting movements, treatment plans,
agent tools, Clerk, inventory, or scheduling redesign was added. Existing
Charge/Payment amount, overpayment, idempotency, audit, and tenant authority
remain the source of truth.

## Pre-flight evidence

The required read-only checks ran against all three canonical databases:

| Database | Alembic | Result |
|---|---:|---|
| `odontoflow` | `0001` | `visits` and `payments` are not present yet; no duplicate check applicable |
| `odontoflow_test` | `0018` | no duplicate `(organization_id, appointment_id)` visits; no duplicate non-null payment references; no methods before test cleanup |
| `odontoflow_e2e` | `0015` | no duplicate visits; historical methods are only `Tarjeta,Yape`, both in the approved migration map; reference columns are not present before migration |

The read-only pre-flight produced no duplicate attendance or payment-reference
rows. The repository's legacy MediStock tree was not modified.

## Migrations

- `0016_visit_appointment_uniqueness`: partial unique
  `uq_visits_org_appointment`.
- `0017_payment_reconciliation`: approved method-label backfill, payment
  metadata/checks, `NOT VALID` historical digital-reference check, partial
  organization/method/reference unique index, and system-only
  `payments.manage` seed.
- `0018_charge_follow_up`: follow-up table/checks/composite tenant FK/indexes
  and system-only `follow_ups.*` permission seeds.

## Verification record

OpenAPI hashes at generation time:

- `docs/api/openapi.yaml`: `a9a521d2b1fea6c3ab840a6916c5e6d69a5770fe07e681ce898fd9e24ff12dba`
- `docs/api/openapi.json`: `6f588128786ec1ac9a761360894798ed5d117468b2e1c4695a5072c3aeea0e42`

Baseline full suite before FE3A production changes: `502 passed, 20 warnings`.
Focused FE3A suite after implementation: `25 passed, 1 warning`.
The final full-suite result and exact commit are reported with the dispatch
completion after the fresh verification run. Final full backend suite:
`527 passed, 20 warnings` (`.venv/bin/python -m pytest -q`).
