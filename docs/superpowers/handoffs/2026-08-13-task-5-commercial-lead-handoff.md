# Task 5 Handoff — Commercial Lead Slice

## 1. Objective

Implement the commercial pre-clinical **Lead** slice: `create_lead(session, data)` and `get_lead(session, lead_id)` application services with a deterministic two-stage boundary (raw input → normalization → validation → persistence), Pydantic v2 schemas, and acquisition-source / at-least-one-contact / active-service-need rules. Lead is the commercial identity that precedes any clinical `Patient` concept — no DNI, no clinical flags.

## 2. Baseline commit

`6069ab5` — `feat: add operational catalog and practitioner eligibility`.

## 3. Resulting commit

**No commit made** — this task was explicitly instructed NOT to commit. Changes remain uncommitted in the working tree alongside the Task 6 availability files (`app/scheduling/availability.py`, `tests/test_availability.py`, `2026-08-13-task-6-availability-handoff.md`), which were NOT touched.

## 4. Files changed (allowed write paths only)

| File | Change |
|---|---|
| `app/commercial/schemas.py` | NEW — `LeadCreate`, `LeadRead` |
| `app/commercial/service.py` | NEW — `create_lead`, `get_lead`, deterministic normalization/validation helpers |
| `tests/test_lead.py` | NEW — 10 TDD cases (see §10) |
| `docs/superpowers/handoffs/2026-08-13-task-5-commercial-lead-handoff.md` | NEW — this document |

No other file was modified. `app/commercial/models.py` (Task 2) is used as-is.

## 5. Lead contract

- `Lead` persists `full_name`, `contact_phone`, `contact_email`, `acquisition_source`, `service_need_id` (FK → `services.id`, RESTRICT), `commercial_status`, `created_at`.
- `commercial_status`: only the existing model default `"new"` is used. **No state machine invented** (spec defers exact progression to a later vertical).
- No `Patient`, no DNI, no clinical fields — per the approved design and the "Excluded" section of `2026-08-12-lead-to-appointment-design.md`.
- Lead deduplication is out of scope (spec §Deferred).

### Schemas

```python
class LeadCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    contact_phone: str | None = None
    contact_email: str | None = None
    acquisition_source: Literal["promotion", "referral", "direct"]
    service_need_id: int | None = None

class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    full_name: str
    contact_phone: str | None
    contact_email: str | None
    acquisition_source: str
    service_need_id: int | None
    commercial_status: str
```

## 6. Normalization behavior

Pure, deterministic, no LLM inference anywhere:

- **Phone** — `re.sub(r"[^\d+]", "", value)`: keeps digits and any leading `+`, strips spaces/dashes/parens/letters. `"+51 999-001-111"` → stored as `"+51999001111"` (verified by test). Post-normalization empty string → `None` (so a phone made only of separators counts as "no phone").
- **Email** — deterministic whitespace `strip()`; empty/whitespace-only → `None` (so it never satisfies the at-least-one-contact rule vacuously).
- **full_name** — unchanged beyond Pydantic length rules (no case folding, no `.title()`; MediStock's title-casing was NOT adopted).
- No missing data is ever inferred or synthesized.

## 7. Error mapping

| Condition | AppError code | HTTP |
|---|---|---|
| Neither `contact_phone` nor `contact_email` after normalization | `INVALID_INPUT` | 422 |
| `acquisition_source` not one of `promotion`/`referral`/`direct` (defensive re-check) | `INVALID_INPUT` | 422 |
| `service_need_id` supplied but `services.id` does not exist | `NOT_FOUND` | 404 |
| `service_need_id` supplied but service inactive | `ENTITY_INACTIVE` | 409 |

The database remains the final authority: `ck_leads_acquisition_source` and `ck_leads_at_least_one_contact` CHECK constraints are exercised by Task 3's `test_schema_constraints.py::test_check_rejects_invalid_acquisition_source` and `::test_check_requires_contact_channel` (still green). The application performs preflight validation for clear errors; the DB constraints guard the same invariants independently.

Validation order: normalize phone/email → validate acquisition source → check at-least-one-contact → check service need exists+active → persist.

## 8. MediStock reference decisions (read-only, `/tmp/opencode/medistock-ref/`)

Files inspected exactly and unmodified: `data_curation_service.py`, `paciente_schema.py`.

| Item | Decision | Evidence / reason |
|---|---|---|
| Phone normalization `re.sub(r"[^\d+]", ...)` | **ADAPT** | `data_curation_service.py:31-35` `_normalize_phone` — identical intent; no leading-`+` constraint needed. |
| normalize-then-validate pipeline | **ADAPT** | `data_curation_service.py:38-88` `curate_paciente_payload` collects normalized fields first, then checks required-ness. OdontoFlow uses the same shape via `_normalize_phone`/`_normalize_contact_email` → `_validate_acquisition_source` → persistence. |
| Mandatory 8-digit DNI | **DO NOT COPY** | `paciente_schema.py:41-44` `dni = fields.String(required=True, validate=validate.Regexp(r'^\d{8}$'))` and `data_curation_service.py:22-28` `_normalize_dni`. OdontoFlow's `Lead` deliberately has no DNI (Peru-specific state identity; clinical). |
| `nombre_completo` min 3 + `.title()` casing | **DO NOT COPY** | `paciente_schema.py:45-48`, `data_curation_service.py:59`. OdontoFlow accepts any non-empty name (min_length 1) and never case-folds. |
| `sexo` M/F/O mapping and `paciente_problematico` clinical flags | **DO NOT COPY** | `paciente_schema.py:49-51`, `data_curation_service.py:66-86`. Clinical/patient-only; out of scope for the commercial Lead identity. |
| `curate_consulta_payload`, `data_quality_snapshot` | **DO NOT COPY** | `data_curation_service.py:90-145`. Consult/quality reporting for the legacy patient system. |
| Marshmallow schemas + Flask-SQLAlchemy `db.session`, legacy `.get()` dict pipeline | **DO NOT COPY** | `paciente_schema.py` (marshmallow), `data_curation_service.py:7` (`app.extensions import db`). OdontoFlow uses Pydantic v2 + SQLAlchemy 2.0 explicit `Session` (matching `app/catalog/service.py` / `app/organization/service.py` style). |

No MediStock code was copied verbatim; no `../medistock` file was modified (read-only access).

## 9. Design decisions worth recording

- `LeadCreate.acquisition_source` is `Literal["promotion","referral","direct"]`, so Pydantic rejects anything else at construction time (422 envelope). The service re-checks the literal set defensively so a schema-bypassed payload (e.g. `model_construct`) still raises a stable `AppError INVALID_INPUT` instead of reaching the DB. Test 5 exercises exactly that bypass path.
- `_validate_service_need` uses `session.get(Service, id)` for existence then `is_active` for state — same rule-check-then-persist pattern as Task 4's eligibility.
- `get_lead` raises `AppError NOT_FOUND` for an unknown id, consistent with the Task 3 error contract.
- Contact email intentionally not validated as an email format (no `EmailStr`): the spec defines at-least-one-contact, not format. Pydantic field stays `str | None` to avoid an extra validator dependency not required by the plan.

## 10. TDD cases + results

Tests written FIRST (`tests/test_lead.py`); initial run failed at collection (`ModuleNotFoundError: No module named 'app.commercial.schemas'`), then implementation made them green.

```
.venv/bin/python -m pytest tests/test_lead.py -q   →   10 passed
```

| # | Test | Expectation |
|---|---|---|
| 1 | `test_valid_direct_lead_with_phone_persists` | direct + phone persists; `get_lead` returns it; `commercial_status == "new"` |
| 2 | `test_valid_referral_lead_with_email_persists` | referral + email persists, phone None |
| 3 | `test_valid_promotion_lead_with_service_need_persists` | promotion + `service_need_id` (Service created via `app.catalog.service.create_service`) persists and is read back |
| 4 | `test_neither_phone_nor_email_rejected` | `AppError INVALID_INPUT`, nothing persisted |
| 5 | `test_unsupported_acquisition_source_rejected` | schema-bypassed `walkin` → `AppError INVALID_INPUT` |
| 6 | `test_phone_normalization_deterministic` | `'+51 999-001-111'` stored as `'+51999001111'` |
| 7 | `test_missing_service_need_raises_not_found` | unknown `service_need_id` → `AppError NOT_FOUND` |
| 8 | `test_inactive_service_need_raises_entity_inactive` | `service.is_active = False` + commit → `AppError ENTITY_INACTIVE` |
| 9 | `test_no_service_need_persists_with_null` | no `service_need_id` → NULL persisted |
| 10 | `test_lead_distinct_from_patient` | `leads` table exists; no `patients`/`pacientes`/`patient`/`lead_patients` table (pg_tables query) |

## 11. Regression confirmation

Full suite from repo root:

```
.venv/bin/python -m pytest -q   →   61 passed, 6 warnings in 6.06s
```

- 39 prior tests (Task 1 health/migrations, Task 2 booking invariant + schema constraints, Task 3 error contract, Task 4 catalog+organization) — green.
- 12 Task 6 availability tests — green, untouched.
- 10 new Task 5 lead tests — green.

## 12. Confirmation: no shared files modified

Only the four allowed write paths were written. Nothing was touched in `scheduling/`, `catalog/`, `organization/`, `audit/`, `migrations/`, `app/errors.py`, `app/db.py`, `app/config.py`, `tests/conftest.py`, `tests/test_schema_constraints.py`, or any other file. No git commit was created. Task 6's untracked files were left exactly as found.

## 13. Blockers

None.

## 14. Risks

- **Acquisition-source race**: app preflight + DB CHECK enforce the invariant; a concurrent path that bypasses the app still fails at the DB CHECK (IntegrityError → generic 500 until a deterministic `23505` translation is added). Same known pattern as Task 4 duplicates.
- **Email format not validated** (by design): a syntactically invalid email would persist. Revisit with `EmailStr` if the commercial team wants format enforcement in a later task.
- **Phone normalization keeps a leading `+`**: `+51999001111` is stored with the country code as entered; no canonical country-code policy was imposed. Fine for this vertical, document if WhatsApp integration later requires a strict E.164 shape.
- **No dedup / no commercial_status transitions**: both are explicitly deferred in the approved spec; do not build them in Task 7.

## 15. Context for Task 7

Task 7 (per the vertical) will consume `get_lead` and a lead's active `service_need` to drive slot search and booking. Expect to reuse `app.commercial.models.Lead` + `get_lead(session, lead_id)` as the booking root, and to enforce the active-service-requirement already implemented here via `_validate_service_need` (the same `NOT_FOUND` / `ENTITY_INACTIVE` order is what scheduling must re-check inside its transaction). The `LeadRead` schema is ready to be returned from FastAPI routers (Task 8 owns API integration, per the Task 4 handoff).
