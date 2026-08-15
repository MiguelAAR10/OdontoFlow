# PF2 — Principal & Authorization · Handoff

**Date:** 2026-08-14 · **Baseline SHA:** `4ff2de5` (217 PASS) · **Result:** `258 passed` · **Not committed**
**Authority:** `docs/superpowers/specs/2026-08-14-platform-foundation-design.md` (PF0), §10–§12, §13 X8, §21 (PF2 block)
**Nature:** IMPLEMENTATION. New migration `0003`, new `app/iam/` domain, no behaviour change for Vertical 1 or PF1.

---

## 1. Schema added (migration `0003`, six new tables, strictly additive)

No existing table, column, constraint or row is altered. `excl_appointments_confirmed_no_overlap` and every
PF1 composite FK are untouched.

| Table | Ownership | Columns | Keys |
|---|---|---|---|
| `principals` | **GLOBAL** (no `organization_id`) | `id`, `type`, `display_name`, `external_subject` NULL, `is_active`, `created_at` | `CHECK type IN ('human','agent','integration','system')`; `UNIQUE (external_subject)` |
| `memberships` | direct `organization_id` NOT NULL | `id`, `organization_id`, `principal_id`, `is_active`, `created_at` | `UNIQUE (organization_id, principal_id)`; `UNIQUE (organization_id, id)`; FKs → organizations, principals |
| `permissions` | **PLATFORM** catalog (no `organization_id`) | `id`, `code`, `name` | `UNIQUE (code)`; seeded by migration only |
| `roles` | direct `organization_id` NOT NULL | `id`, `organization_id`, `code`, `name`, `created_at` | `UNIQUE (organization_id, code)`; `UNIQUE (organization_id, id)` |
| `role_permissions` | derived (none needed) | `id`, `role_id`, `permission_id` | `UNIQUE (role_id, permission_id)` |
| `role_assignments` | direct `organization_id` NOT NULL | `id`, `organization_id`, `membership_id`, `role_id`, `location_id` **NULL**, `created_at` | 3 composite FKs + 2 partial unique indexes (below) |

Indexes added: `ix_memberships_principal`, `ix_role_permissions_permission`,
`uq_role_assignment_scoped`, `uq_role_assignment_org_wide`. Every FK is `ON DELETE RESTRICT` — no CASCADE,
no SET NULL — consistent with `0001`/`0002` and asserted by `test_every_pf2_foreign_key_restricts_deletion`.

**Seeded by the migration, and nothing else:**

1. The seventeen §11 M7 permission codes (`appointments.{read,create,reschedule,cancel}`,
   `services.{read,manage}`, `leads.{read,create}`, `locations.{read,manage}`,
   `practitioners.{read,manage}`, `capabilities.{read,manage}`, `availability.{read,manage}`,
   `audit.read`).
2. The `system` principal (PR6), id pinned to `1`, identity sequence advanced past it.
3. For **every organization that already exists** (PR7): a `system` role holding the whole catalog, the
   system principal's membership, and its organization-wide role assignment.

`downgrade()` drops the six tables in dependency order; `0002 → 0001 → 0002 → 0003` is exercised by the
migration tests.

---

## 2. DB invariants (PostgreSQL rejects these, with every application check bypassed)

Each is proven by a raw-SQL insert in `tests/test_authorization.py` — no application service participates.

| # | Impossible state | Mechanism | Test |
|---|---|---|---|
| 1 | A fifth kind of actor | `ck_principals_type` | `test_unknown_principal_type_is_rejected_by_the_database` |
| 2 | Two principals sharing one auth subject | `uq_principals_external_subject` | `test_external_subject_is_unique_but_many_principals_may_have_none` |
| 3 | Duplicate Principal × Organization membership | `uq_memberships_organization_principal` | `test_duplicate_membership_is_rejected_by_the_database` |
| 4 | **Organization B's role assigned inside A** (F-6) | `fk_role_assignments_organization_role` → `roles(organization_id, id)` | `test_a_role_from_another_organization_cannot_be_assigned` |
| 5 | **Organization B's membership assigned inside A** | `fk_role_assignments_organization_membership` → `memberships(organization_id, id)` | `test_a_membership_from_another_organization_cannot_be_assigned` |
| 6 | **Assignment scoped to another tenant's Location** (F-3) | `fk_role_assignments_organization_location` → `locations(organization_id, id)` | `test_an_assignment_cannot_be_scoped_to_another_tenants_location` |
| 7 | A tenantless assignment | `organization_id` NOT NULL | `test_a_role_assignment_needs_a_tenant` |
| 8 | Duplicate **organization-wide** assignment | partial index `uq_role_assignment_org_wide` | `test_duplicate_org_wide_assignment_is_rejected` |
| 9 | Duplicate **scoped** assignment | partial index `uq_role_assignment_scoped` | `test_duplicate_scoped_assignment_is_rejected` |
| 10 | Duplicate role → permission edge | `uq_role_permissions_role_permission` | `test_duplicate_role_permission_is_rejected` |
| 11 | Two roles with the same code in one organization | `uq_roles_organization_code` | `test_duplicate_role_code_inside_one_organization_is_rejected` |

**Cross-table tenant equality is structural, not a trigger.** `role_assignments` carries `organization_id`
and references membership, role and location **by that same column** (§7.1), so
`role.organization_id == membership.organization_id == location.organization_id` is true by construction.
No trigger, no RLS policy and no application check participates in invariants 4–6.

**MATCH SIMPLE, never MATCH FULL** (§7.3). All three composite FKs use PostgreSQL's default, so a NULL
`location_id` skips the location check — that is exactly what encodes "organization-wide".
`test_an_org_wide_assignment_is_accepted_by_the_nullable_composite_fk` proves the NULL case is accepted, so
a future MATCH FULL would fail the suite rather than silently break the scope model.

---

## 3. Authorization evaluation contract

```python
has_permission(session, principal_id, organization_id, permission_code, location_id=None) -> bool
require_permission(session, ctx: ExecutionContext, code, *, location_id=None) -> None   # raises on denial
effective_permission_codes(session, principal_id, organization_id, location_id=None) -> set[str]
```

One SQL statement decides everything (`app/iam/service.py`), exactly §12's conceptual query:

```sql
SELECT 1
  FROM memberships m
  JOIN role_assignments ra ON ra.membership_id = m.id
  JOIN role_permissions rp ON rp.role_id       = ra.role_id
  JOIN permissions p       ON p.id             = rp.permission_id
 WHERE m.organization_id = :organization_id
   AND m.principal_id    = :principal_id
   AND m.is_active
   AND p.code            = :code
   AND (ra.location_id IS NULL OR ra.location_id = :location_id)
 LIMIT 1;
```

- **E1/A9 deny by default** — no matching row is a denial; an unknown permission code is a denial, not an
  error; a principal with no membership is a denial.
- **E2/E3 live evaluation** — nothing is cached, memoized or precomputed; there is no permission set in
  `ExecutionContext` (X4) and no token to expire.
- **E6 single entry point** — `require_permission` is the only guard; it is designed to be the first
  statement of an application command service. PF2 wires no transport (that is PF3, §21).
- **E9 error** — denial raises `AppError` with code `PERMISSION_DENIED`, HTTP **403**, `details == {}`, in
  the existing stable envelope. See §7 for why the code lives in `app/iam/service.py` for now.
- **P7 one mechanism for every actor** — no branch on `principal_type` exists in the evaluation path;
  `test_the_evaluation_never_branches_on_principal_type` asserts the source contains no principal-type
  literal at all.
- **M9 no role-name logic** — `roles.code`/`roles.name` are never read by a decision.
  `test_no_role_name_logic_anywhere_in_application_code` parses every module under `app/` with `ast` and
  fails on any code-level string literal in `{owner, admin, administrator, superuser, manager,
  receptionist, staff}` (comments and docstrings excluded, so prose cannot trip it and a real branch cannot
  hide in one).
- **M5 catalog is code-owned** — no application surface inserts into `permissions`; `grant_permission`
  resolves an existing code and raises `LookupError` on an unknown one.
  `test_no_application_surface_writes_the_permission_catalog` enforces it.
- **M8 no implication** — `services.read` does not grant `services.manage`; the guard is a pure
  set-membership test.
- **E7** — no permission decision reads model output; the only inputs are the principal id and the
  organization's membership/role rows.

---

## 4. Organization-wide vs location scope

| Assignment | `location_id=None` check (org-level operation) | check at location A | check at location B |
|---|---|---|---|
| `location_id IS NULL` (organization-wide) | **allow** | **allow** | **allow** |
| `location_id = A` | **deny** (E5) | **allow** | **deny** (E4) |

Scope is **concrete, not polymorphic** (M1): the single scope dimension is a nullable `location_id` FK, so
PostgreSQL can check it. A `scope_type`/`scope_id` pair could not be constrained by a foreign key, which is
the whole reason §7 exists.

A location-scoped grant never widens: it is denied for location-less operations
(`test_location_scoped_assignment_denies_a_location_less_operation`), and the same role may be scoped to
several locations independently without ever becoming organization-wide
(`test_the_same_role_may_be_scoped_to_several_locations`).

**Operational consequence of E5, carried forward (BLOCKER-2).** `Lead` has no `location_id`, so
`leads.create` is a location-less operation and only an organization-wide grant satisfies it. A
branch-scoped receptionist therefore cannot register a lead today. PF0 §23 recommends option (a) — an
additive nullable `leads.location_id` — and explicitly requires PF2 to document the consequence if the
answer is (b). PF1 shipped without the column, so the consequence stands and is recorded here for the block
that resolves it.

---

## 5. Inactive membership behaviour

`memberships.is_active` is re-read by every evaluation, inside the command's transaction (E2/E3, F-5):

- an inactive membership resolves **zero** effective permissions —
  `effective_permission_codes(...) == set()` — regardless of how many role assignments reference it;
- the role assignments are **not** deleted: authority is lost through the membership and returns intact when
  the membership is reactivated (asserted in both directions);
- revocation takes effect on the **next** command: there is no cache to invalidate, no token to expire, and
  no permission set in the context to go stale;
- the same holds for the seeded `system` principal — deactivating its membership denies it immediately,
  which is the proof that platform automation has no bypass path (PR7/P7).

`principals.is_active` (PR5, the platform-wide kill switch) is deliberately **not** part of the evaluation
query: PF0 places it at identity resolution, before authorization, and identity resolution is PF3. See §9.

---

## 6. Tests (`tests/test_authorization.py`, 41 tests — all PASS)

Real PostgreSQL throughout, no mocks. Database proofs write raw SQL; evaluation proofs construct
`ExecutionContext` directly (X7), since PF2 adds no HTTP enforcement.

| Required proof (task brief / PF0 §21) | Test(s) |
|---|---|
| 1. Four principal types valid; invalid type rejected by the DB | `test_every_closed_set_principal_type_is_accepted[human/agent/integration/system]`, `test_unknown_principal_type_is_rejected_by_the_database` |
| 2. One principal in two organizations | `test_one_principal_belongs_to_two_organizations` |
| 3. Duplicate membership rejected | `test_duplicate_membership_is_rejected_by_the_database` |
| 4. Inactive membership yields no permission | `test_inactive_membership_resolves_zero_effective_permissions` |
| 5. Same role code in two organizations accepted | `test_the_same_role_code_may_exist_in_two_organizations`, `test_duplicate_role_code_inside_one_organization_is_rejected` |
| 6. RolePermission resolves the permission; grant/deny matrix | `test_grant_and_deny_matrix_per_permission_code`, `test_permission_denied_uses_the_stable_envelope_contract` |
| 7. Org-wide assignment authorizes at any location | `test_org_wide_assignment_authorizes_at_every_location` |
| 8. Location-scoped authorizes location A | `test_location_scoped_assignment_authorizes_only_its_own_location` |
| 9. Same assignment denies location B | `test_location_scoped_assignment_denies_another_location`, `test_location_scoped_assignment_denies_a_location_less_operation`, `test_the_same_role_may_be_scoped_to_several_locations` |
| 10. Cross-org role assignment rejected by the DB | `test_a_role_from_another_organization_cannot_be_assigned`, `test_a_membership_from_another_organization_cannot_be_assigned` |
| 11. Cross-org scoped location rejected by the DB | `test_an_assignment_cannot_be_scoped_to_another_tenants_location` |
| 12. Human and agent evaluated identically | `test_human_and_agent_with_identical_assignments_resolve_identical_authority`, `test_the_evaluation_never_branches_on_principal_type` |
| 13. No hardcoded role-name authorization | `test_renaming_a_role_does_not_change_authorization`, `test_no_role_name_logic_anywhere_in_application_code`, `test_the_authorization_query_reads_no_role_name_column` |
| M4 duplicate assignments (org-wide and scoped) | `test_duplicate_org_wide_assignment_is_rejected`, `test_duplicate_scoped_assignment_is_rejected`, `test_duplicate_role_permission_is_rejected` |
| M5–M8 catalog is code-owned, convention-conformant, non-implying | `test_the_seeded_catalog_is_exactly_the_m7_closed_set`, `test_every_permission_code_follows_the_naming_convention`, `test_no_application_surface_writes_the_permission_catalog` |
| PR6/PR7 seeded system principal | `test_the_migration_seeds_exactly_one_system_principal`, `test_the_system_principal_is_permission_checked_like_any_other`, `test_provision_system_access_grants_a_new_organization_and_is_idempotent` |
| §7.3 MATCH SIMPLE / RESTRICT / schema facts | `test_an_org_wide_assignment_is_accepted_by_the_nullable_composite_fk`, `test_a_role_assignment_needs_a_tenant`, `test_pf2_constraints_exist_in_postgresql`, `test_every_pf2_foreign_key_restricts_deletion` |
| §13 X2/X4 context is frozen and authority-free | `test_the_execution_context_is_frozen_and_carries_no_authority`, `test_principals_are_global_and_carry_no_tenant_column` |

---

## 7. Deviations from the task brief (deliberate, spec-driven)

1. **`roles` is keyed on `code`, not `name`.** The brief suggested `UNIQUE (organization_id, name)`; PF0 §6
   and §11 specify `UNIQUE (organization_id, code)` with `code` and `name` as separate columns. The spec is
   the named authority; the tested behaviour (same role in two organizations is legal, twice inside one is
   not) is identical either way.
2. **`principals` columns follow §10 exactly**: `type` (not `principal_type`) and `display_name` (not
   `name`), plus `external_subject` and `is_active`, which the brief did not list but §10 requires.
3. **`PERMISSION_DENIED` is declared in `app/iam/service.py`, not in `app/errors.py`.** PF0 §21 puts the code
   in the error contract, but the task brief lists `app/errors.py` under DO NOT modify. It is raised as
   `AppError(IamErrorCode.PERMISSION_DENIED, message, details={}, http_status=403)` — both lookup tables in
   `AppError.__init__` are short-circuited by the explicit arguments, and the registered handler renders
   `code.value`, so the wire format is byte-identical to every other error:
   `{"error": {"code": "PERMISSION_DENIED", "message": ..., "details": {}}}`. **Follow-up for PF3**: move the
   entry into `ErrorCode` / `HTTP_STATUS_BY_CODE` / `DEFAULT_MESSAGE_BY_CODE` and drop `IamErrorCode` — a
   three-line change in the block that already owns `app/errors.py` (it adds `UNAUTHENTICATED` there).
4. **PR7 is provided but not wired into `create_organization`.** PF0 §21 asks organization creation to create
   the system membership in the same transaction; `app/organization/service.py` is under DO NOT modify.
   `provision_system_access(session, organization_id)` implements the invariant, is idempotent, and never
   commits (so it composes into the caller's transaction). The migration already backfilled every existing
   organization. See §9 blocker 1.
5. **`has_permission` takes `session` first**, matching every other service in the codebase; the brief's
   argument order is otherwise preserved.

---

## 8. Files changed

### New

| Path | What |
|---|---|
| `alembic/versions/0003_principal_authorization.py` | migration `0002 → 0003`: six tables, the M7 catalog seed, the `system` principal and its per-organization access; full downgrade |
| `app/iam/models.py` | `Principal`, `Membership`, `Permission`, `Role`, `RolePermission`, `RoleAssignment` + the closed type vocabulary and system constants |
| `app/iam/permissions.py` | the platform permission catalog (M5–M8): codes, display names, naming convention |
| `app/iam/context.py` | `ExecutionContext` — frozen, slots, five fields (§13, type only per X8) |
| `app/iam/service.py` | `has_permission`, `require_permission`, `effective_permission_codes`, the thin provisioning surface, `provision_system_access` |
| `tests/test_authorization.py` | 41 PF2 proofs against real PostgreSQL |

### Adapted (strictly required — schema facts extended, never weakened)

| Path | Change |
|---|---|
| `alembic/env.py` | registers the six `app.iam` models so autogenerate stays truthful (one import block) |
| `tests/conftest.py` | the five tenant-owned IAM tables added to the truncation list (**`permissions` deliberately excluded** — platform catalog); `_seed_system_principal()` restores exactly what `0003` seeds, mirroring PF1's `_seed_bootstrap_organization()` |
| `tests/test_migrations.py` | `EXPECTED_TABLES` gained the six tables; `HEAD_REVISION` `0002 → 0003` |
| `tests/test_tenant_integrity.py` | the "every tenant-owned table has NOT NULL `organization_id`" set gained `memberships`, `roles`, `role_assignments`; new assertions that `principals` (global, T2/PR3) and `permissions` (platform, T3) carry **no** tenant column |

### Not touched (verified by `git status` / `git diff`)

`app/errors.py`, `app/db.py`, `app/tenancy.py`, `app/scheduling/availability.py`,
`alembic/versions/0001_*.py`, `alembic/versions/0002_*.py`, every existing domain service, schema and
router, `app/__init__.py`, the PF0 spec, `../../AI-EdgeRunners/medistock`. **No existing test assertion was weakened or
deleted**, and no request/response schema or OpenAPI path changed.

---

## 9. Blockers / risks

1. **PR7 is half-wired (follow-up, not a defect).** `create_organization` does not yet call
   `provision_system_access`, because `app/organization/service.py` is outside this task's write surface. An
   organization created after `0003` therefore has no system membership until someone calls the function —
   proven and covered by `test_provision_system_access_grants_a_new_organization_and_is_idempotent`, and
   visible as a denial in `test_the_system_principal_is_permission_checked_like_any_other`. **One line inside
   `create_organization`'s transaction closes it**, and it must land before PF4 (a `command_receipt` requires
   membership, §15 I3).
2. **`PERMISSION_DENIED` is not yet in the central error tables** (§7 item 3). It renders correctly today;
   PF3 should fold it in when it adds `UNAUTHENTICATED`.
3. **`principals.is_active` is not part of the evaluation query** — PF0 PR5 enforces it at identity
   resolution, which PF3 owns. Until then, an inactive principal that still holds an active membership would
   evaluate normally. No transport can reach the guard yet, so nothing is exposed; PF3 must not forget the
   check.
4. **E5 + `Lead` has no location** (BLOCKER-2, §4 above): a branch-scoped principal cannot create a lead.
   Unresolved by design; PF0 recommends adding a nullable `leads.location_id`.
5. **No authorization is enforced at the HTTP surface yet** — deliberate (§21 "Explicitly NOT included"):
   PF2 proves the guard at the service layer, PF3 wires transports. Until then the API remains as open as it
   was before this block.
6. **The seeded `system` role holds the whole catalog.** That is what PR7 requires (platform automation must
   be able to act), but it means the `system` principal is a full-authority actor in every organization. Its
   protection is `principals.is_active` plus the fact that nothing can *become* it without identity
   resolution (PF3).

---

## 10. Verification evidence

```
$ .venv/bin/python -m pytest -q
258 passed, 12 warnings
```

217 pre-existing (PF1 baseline `4ff2de5`) + 41 new authorization proofs. Real PostgreSQL 15 throughout
(`docker-compose` service `db`, port 5434), no mocks, no SQLite. Migration `0001 → 0002 → 0003`, downgrade
to `0001` and re-upgrade are exercised by `tests/test_migrations.py` against throwaway databases holding
Vertical 1 rows.

Cross-block invariants (§22) re-verified: `excl_appointments_confirmed_no_overlap` unchanged; the overlap
preflight still not organization-filtered; booking/cancel/reschedule still own their transactions; audit
still written inside the mutation's transaction; no LLM or agent library imported by `app/`.

---

## 11. PF2: CLOSED
