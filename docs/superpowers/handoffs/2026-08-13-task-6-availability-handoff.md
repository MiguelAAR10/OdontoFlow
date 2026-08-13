# Handoff — Task 6: Deterministic Availability (Slot Generation) Core

Date: 2026-08-13

## Objective

Implement the deterministic availability slot-generation core for OdontoFlow:
a pure function that turns recurring availability rules, exceptional schedule
blocks, existing appointments, the canonical service duration and a requested
UTC window into the sorted list of bookable intervals. No persistence, no
network, no LLM, no framework code. This is the "Compute candidate starts"
step (Core Flow step 5) from the lead-to-appointment design spec.

## Baseline commit

`6069ab5` — `feat: add operational catalog and practitioner eligibility`
(39 tests PASS before this task).

## Files changed

- `app/scheduling/availability.py` — new: pure slot-generation core (stdlib only).
- `tests/test_availability.py` — new: 12 TDD tests.
- `docs/superpowers/handoffs/2026-08-13-task-6-availability-handoff.md` — this file.

No other files modified. No commits made.

## Function signature + data shapes

```python
generate_slots(rules, blocks, appointments, duration_minutes,
               window_start, window_end, timezone) -> list[tuple[datetime, datetime]]
```

Returns a strictly chronological, sorted, deduplicated list of
`(start_utc, end_utc)` tuples.

Input dataclasses defined in the module (frozen):

| Type | Fields | Notes |
|---|---|---|
| `AvailabilityRule` | `day_of_week: int`, `start_local: time`, `end_local: time` | `0=Monday..6=Sunday`; wall-clock window `[start_local, end_local)` |
| `ScheduleBlock` | `start_utc: datetime`, `end_utc: datetime` | exceptional closed UTC interval, half-open |
| `Appointment` | `start_utc: datetime`, `end_utc: datetime`, `state: str` | only `state == 'confirmed'` blocks |

For integration convenience the function also accepts plain tuples/lists in the
same positional order, or any object exposing the same attribute names. Naive
datetimes are treated as UTC; aware datetimes are normalized to UTC. Windows,
blocks and appointments are compared in UTC.

## Deterministic rules (implemented)

1. Candidate starts are aligned to a 15-minute grid evaluated on the location's
   wall clock (`09:00`, `09:15`, `09:30`, ...), starting at `rule.start_local`.
2. The whole interval `[start, start + duration)` must fit inside an
   availability window (`start + duration <= end_local`, wall-clock minutes).
3. The interval must not intersect any schedule block.
4. The interval must not intersect any confirmed appointment.
5. Cancelled appointments never block.
6. Back-to-back intervals are allowed (`[start, end)` semantics).
7. Output is independent of input ordering and strictly chronological.

Additionally: the interval must lie within `[window_start, window_end)`. A
non-positive duration or `window_end <= window_start` returns `[]`.

## Half-open semantics notes

- All intervals (`[start_local, end_local)`, `[start_utc, end_utc)`) are
  half-open: an end equals another interval's start means no intersection.
- A slot that ends exactly at the availability window end is valid
  (e.g. `[09:15, 10:00)` inside `[09:00, 10:00)`).
- Intersection test used: `a.start < b.end and b.start < a.end`.

## Timezone / DST handling

- `timezone` is an IANA name resolved with `zoneinfo.ZoneInfo` (no `pytz`).
- Local wall-clock times are combined with `tzinfo=ZoneInfo(...)` and converted
  to UTC via `astimezone`. Python's fold semantics make ambiguous/nonexistent
  local times deterministic (fold=0).
- Per UTC day in the requested window, candidates are generated for **both**
  local dates that the UTC day touches (`local_date(utc-midnight)` and that
  date + 1 day). This is required because a UTC day can straddle a local-midnight
  crossing anywhere within it; probing only UTC-midnight missed e.g. a Lima
  local date whose midnight falls later in the UTC day (caught by
  `test_location_timezone_respected`).
- Tested across the US DST boundary (`America/New_York`, 2026-03-01 EST vs
  2026-03-08 EDT): the same local 09:00 window yields `14:00Z` pre-DST and
  `13:00Z` post-DST, asserted as exact instants.

## TDD cases + results

All 12 cases were written first; the suite failed with
`ModuleNotFoundError: No module named 'app.scheduling.availability'` (RED), then
passed after implementation (GREEN).

1. `test_30_min_service_on_15_minute_grid`
2. `test_duration_not_divisible_by_15_uses_grid_and_fit_check`
3. `test_candidate_extending_past_availability_end_is_rejected`
4. `test_schedule_block_removes_intersecting_candidates`
5. `test_confirmed_appointment_removes_intersecting_candidates`
6. `test_cancelled_appointment_does_not_block`
7. `test_back_to_back_intervals_permitted`
8. `test_multiple_recurring_windows_generate_candidates`
9. `test_location_timezone_respected`
10. `test_dst_boundary_produces_exact_instants`
11. `test_input_ordering_does_not_affect_output`
12. `test_output_stable_and_chronologically_sorted`

Results: `12 passed` for `tests/test_availability.py`; full suite `51 passed`.

## Purity statement

`app/scheduling/availability.py` is a pure module: it performs no I/O, no
database access, no network calls, and has no mutable module state. It imports
only `dataclasses`, `datetime` and `zoneinfo` from the standard library. Given
identical inputs it returns identical output (asserted by
`test_output_stable_and_chronologically_sorted`).

## Regression confirmation

`.venv/bin/python -m pytest -q` from the repo root: **51 passed** (39 prior
tests + 12 new). The only warnings are a pre-existing Alembic
`DeprecationWarning` originating in `tests/conftest.py`.

## Confirmation: no other files modified

`git status --short` shows exactly two untracked files:
`app/scheduling/availability.py` and `tests/test_availability.py`. No tracked
file was edited; no commit was made.

## Blockers

1. **Task spec case 1 is internally inconsistent with rules 2/3.** The task
   lists, for a 30-minute service over availability `09:00-10:00` local, the
   starts `09:00, 09:15, 09:30, 09:45`. But `[09:45, 10:15)` extends past
   `10:00`, which violates rule 2 ("The whole interval `[start, start+duration)`
   must fit inside an availability window"), case 2, and case 3 ("candidate
   extending past availability end is rejected"). There is no implementation
   that satisfies both case 1 as written and rules 2/3.
   **Resolution applied:** the design doc is authoritative
   ("retain only intervals that fit wholly within recurring availability"), so
   case 1 yields `09:00, 09:15, 09:30` (start `09:45` is rejected). If the
   orchestrator intended the literal 4-start list, the availability window must
   instead be `09:00-10:15` or the duration 15 minutes; revisit before Task 7.

## Risks

- **Ambiguous local times (fall-back DST):** local times in the repeated hour
  resolve deterministically via fold=0 (earlier offset). No test exercises a
  fall-back boundary; the DST test uses the spring-forward boundary. Add a
  fall-back test in Task 7 if fall-back-window bookings are expected.
- **Nonexistent local times (spring-forward gap):** a rule spanning the gap
  (e.g. `02:00-03:30` local) converts via pre-transition offset; the resulting
  instants are deterministic but should be reviewed if such rules are allowed.
- **Very long windows** are O(days × rules × ticks) — trivially cheap for
  realistic horizons; no performance concern today.
- **Duplicate rules** (same weekday + same window) are deduplicated by the
  result set; overlapping-but-identical rules do not duplicate slots.

## Context for Task 7

Task 7 (slot query / booking use case) can compose this core with the
persistence models already present in `app/scheduling/models.py`
(`AvailabilityRule`, `ScheduleBlock`, `Appointment`), which use `day_of_week`,
`start_local`/`end_local` (`Time`), `start_utc`/`end_utc`, and `state` — shapes
that map 1:1 onto the dataclass/tuple coercion supported here. Remaining work
for the vertical: eligible-practitioner filtering, slot query surface,
concurrency-safe confirm (DB exclusion constraint), cancel/reschedule, and the
audit trail. Reconsider the case-1 blocker above (and the deferred minimum
lead-time / maximum horizon questions) when defining the query use case.
