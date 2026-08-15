# OdontoFlow Changelog

## PF4 — Idempotent Commands (2026-08-15)

- Added `command_receipts` table (migration `0004`): durable exactly-once
  execution for `appointments.book`, `appointments.reschedule` and
  `appointments.cancel`, keyed by `(organization_id, operation,
  idempotency_key)` with a canonical request fingerprint.
- Added `app/idempotency/` — the application-level command handler:
  claim-first ordering inside the existing service transactions, replay of
  the stored logical outcome on identical retries, deterministic
  `IDEMPOTENCY_KEY_REUSED` (409) on fingerprint/principal mismatch.
- Transport reads the optional `Idempotency-Key` header and signals replays
  with the non-authoritative `Idempotent-Replay: true` header.
- Agents and integrations must supply an idempotency key (`INVALID_INPUT`
  422); humans keep the previous contract; absent key writes no receipt.
- The practitioner-global GiST exclusion and the existing `23P01`/`40P01`
  behaviour are unchanged.
