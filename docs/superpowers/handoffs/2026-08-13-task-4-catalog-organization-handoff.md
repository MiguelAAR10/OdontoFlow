# Task 4 Handoff — Operational Catalog + Practitioner Eligibility

## 1. Objective

Establish OdontoFlow's canonical operational catalog (Service) and organization (Location, Practitioner, PractitionerCapability) with deterministic eligibility — the base that Scheduling consumes now and Clinical/Finance/Operations/Agents reference later. No pricing, no routers, no booking logic.

## 2. Baseline commit

`084a8d5` (Task 3 — stable API error contract).

## 3. Resulting commit

See `feat: add operational catalog and practitioner eligibility` (this task's single commit, includes this handoff).

## 4. Spec ↔ schema parity finding

**PASS — no gap.** Verified from source (migration `0001_lead_to_appointment.py` lines 26/36/44/54 and `app/catalog/models.py`, `app/organization/models.py`): `is_active` exists with `NOT NULL` + `server_default true` on all four concepts — `services`, `locations`, `practitioners`, `practitioner_capabilities`. The approved design requirement (all four active for eligibility) is satisfied by the existing schema.

## 5. Migration 0002

**NOT created.** Not necessary: parity verified above. No schema change was required.

## 6. Schema changes

None. Task 2's `0001` model is used as-is.

## 7. Canonical Service contract

- Stable identifier (`id`).
- Unique authoritative name (`name` UNIQUE — DB authoritative).
- Authoritative `duration_minutes` (DB CHECK `> 0`; Pydantic `Field(gt=0)`; scheduling must use catalog duration, never client input).
- Active state (`is_active`).
- **No pricing.** `precio_servicio` from MediStock deliberately NOT carried over.
- `create_service(session, ServiceCreate)` rejects duplicates with `AppError INVALID_INPUT`; `list_services(session)` ordered by name.

## 8. Canonical Location contract

- Stable identifier, `name`, `timezone` (IANA, validated with Python `zoneinfo` — `ZoneInfoNotFoundError` → `AppError INVALID_INPUT` **before persistence**), active state.
- Not Peru-hardcoded: `America/Lima`, `Europe/Madrid`, `UTC`, `America/Argentina/Buenos_Aires` all accepted.

## 9. Practitioner / Capability contract

- `Practitioner`: identifier, `display_name`, active state.
- `PractitionerCapability` = exactly Practitioner × Service × Location (+ `is_active`). FK existence checked first (`NOT_FOUND`); duplicate combination rejected by the database unique constraint `uq_capabilities_practitioner_service_location` (authoritative, per plan).

## 10. Eligibility behavior

`list_eligible_practitioners(session, service_id, location_id)`:

1. Service missing → `NOT_FOUND`.
2. Location missing → `NOT_FOUND`.
3. Service inactive → `ENTITY_INACTIVE`.
4. Location inactive → `ENTITY_INACTIVE`.
5. Returns practitioners where practitioner active AND matching capability active AND capability.practitioner/service/location matches — all deterministic, no LLM, no fuzzy matching, no availability calculation.

## 11. MediStock files inspected (targeted)

- `../../AI-EdgeRunners/medistock/src/clinica_backend/app/models/servicio.py`
- `../../AI-EdgeRunners/medistock/src/clinica_backend/app/models/servicio_catalogo.py`
- `../../AI-EdgeRunners/medistock/src/clinica_backend/app/services/catalogo_service.py`
- (schema dir listing: `catalogo_schema.py` exists; not required further)

Finance/inventory/OLAP/Streamlit/agents/routes NOT inspected.

## 12. REFERENCE / ADAPT / NOT COPIED decisions

| Item | Decision | Reason |
|---|---|---|
| Table shape `id + unique name` | **ADAPT** | Seed for `services`; added `duration_minutes` + `is_active`, dropped `precio_servicio` |
| Rule-check-then-persist service pattern | **ADAPT** | Pre-check duplicate → `AppError` instead of `ValueError`; commit/refresh explicit |
| `Servicio` + `ServicioCatalogo` duplicate classes | **NOT COPIED** | Legacy duplication mapping one table (latent inconsistency) |
| `CatalogoService` code (`filter_by(nombre_marca=['nombre_marca'])`, `Marca.nonombre_marca`, f-string missing brace) | **NOT COPIED** | Verified runtime bugs in source |
| `precio_servicio`/pricing assumptions | **NOT COPIED** | Finance owns pricing later |
| `Marca.query`/Flask-SQLAlchemy query API | **NOT COPIED** | SQLAlchemy 2.0 `select()`/`session.get()` |

No MediStock code copied verbatim.

## 13. Files changed

| File | Change |
|---|---|
| `app/catalog/schemas.py` | NEW — `ServiceCreate`, `ServiceRead` |
| `app/catalog/service.py` | NEW — `create_service`, `list_services` |
| `app/organization/schemas.py` | NEW — `LocationCreate/Read`, `PractitionerCreate/Read`, `CapabilityCreate/Read` |
| `app/organization/service.py` | NEW — `create_location` (zoneinfo validation), `create_practitioner`, `create_capability`, `list_eligible_practitioners` |
| `tests/test_catalog_organization.py` | NEW — 17 TDD cases |

No routers (Task 8 owns API integration). No models changed.

## 14. Tests executed + results

`python -m pytest -q` → **39 passed** (22 prior + 17 new). New coverage:

- Service: valid persist; duration `<= 0` rejected (schema validation, DB CHECK from Task 2); duplicate name safe rejection; listing; inactive excluded from eligibility (`ENTITY_INACTIVE`).
- Location: valid IANA timezones accepted (4 zones incl. non-Peru); invalid `Peru/Lima` rejected **before persistence**; inactive excluded (`ENTITY_INACTIVE`).
- Practitioner: active participates; inactive excluded.
- Capability: matching active → eligible; other service → not; other location → not; inactive capability → not; inactive practitioner → not; duplicate → DB `IntegrityError` (unique constraint, verified after `session.rollback()`); missing reference → `NOT_FOUND`.
- Missing service/location → `NOT_FOUND`.

## 15. Migration verification

N/A — migration 0002 not needed (documented in §4-5). No Alembic changes this task.

## 16. Task 2 + Task 3 regression confirmation

Green in the same run: GiST overlap invariant suite (`test_booking_invariant.py`, 4 tests) and error contract suite (`test_errors.py`, 7 tests) pass unchanged.

## 17. MediStock untouched

`git -C ../../AI-EdgeRunners/medistock status` clean at `ef2fffb7a348aa621f7a5b387e09a1553351000f`; only read access this task.

## 18. Blockers

None.

## 19. Risks

- `create_capability` duplicate race relies on the DB unique constraint (correct, but a race produces an `IntegrityError` → generic 500 until a deterministic 23505 translation is added in a later task).
- Eligibility reads `is_active` at query time; no snapshots or history (fine for this vertical).
- Service name case-sensitivity: exact match check (`=`); MediStock used `ilike` — case-insensitive uniqueness deliberately not adopted; document if the business wants it.

## 20. Recommended next activity

**Task 5 — Commercial Lead slice** (per plan): `app/commercial/service.py` + schemas with the two-stage normalization pipeline (ADAPT from `data_curation_service.py` phone normalization), acquisition-source validation, and `service_need_id` association — test-first, using the Task 3 envelope.
