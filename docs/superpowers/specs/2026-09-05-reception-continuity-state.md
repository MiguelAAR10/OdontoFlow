# Reception continuity state — scoped design

## Scope

Persist a small, typed conversational checkpoint with the outgoing message so
the next reception turn can continue an already-started task. This is advisory
state, not an appointment, a proposal or evidence of patient confirmation.
`pending_action` remains the authority for any outstanding proposal. Booking,
reschedule, cancellation, confirmation and handoff authorization rules do not
change in this task. No infrastructure or provider configuration changes.

## Contract

`POST /internal/conversations/{id}/outbound` accepts optional `reception_state`:

- `schema_version`: `reception-state-v1`.
- `intent`: `booking`, `reschedule`, `cancel`, or null.
- `phase`: `discovery`, `awaiting_slot`, `awaiting_name`,
  `awaiting_confirmation`, `completed`, or `handoff`.
- Optional positive integer `service_id`, `location_id`, `appointment_id`.
- Optional offset-aware `window_start`, `window_end`.
- `offered_slots`: at most three `{practitioner_id, start}` objects;
  `selected_slot`: one such object or null. IDs are positive integers and starts
  are offset-aware ISO timestamps.
- Required offset-aware `updated_at` and positive integer `last_message_id`.
  The source message must be a visible inbound in this tenant/conversation.

Unknown fields are rejected at every nested level: no names, raw messages,
credentials or confirmation tokens belong in this checkpoint. Legacy clients
that submit only `text` remain valid. These IDs describe conversational context;
they do not establish domain relationships or authorize mutations.
Omitted/null state does not erase earlier checkpoints. A completed task writes
an explicit `completed` checkpoint with null intent and empty optional fields.

## Persistence and reads

Store the checkpoint as `_reception_state` inside `OutboundMessage.payload`,
in the existing message/outbound/audit transaction. No migration. Idempotent
replays return the existing receipt; reusing the key with a different checkpoint
is a conflict. Failed persistence leaves neither the message nor its checkpoint.

The outbound claim strips the internal checkpoint before returning the provider
payload. No internal state appears in `Message.body_text`, audit content, or the
outbound receipt.
An internal `_reception_state_fingerprint` digest preserves idempotent comparison
after retention removes checkpoint content. The dispatcher strips all private
payload keys, including this digest.

`get_reception_context` returns `reception_state` (object or null), read from the
latest outbound carrying a valid checkpoint whose message is unexpired and not
redacted. Source inbound visibility is also required. All lookups are tenant-
and conversation-scoped. Invalid legacy metadata returns null rather than
breaking the context request. Retention redaction removes the internal checkpoint
from the outbound payload when its owning message is redacted.

## Verification

Real PostgreSQL tests cover backward compatibility, validation, atomicity,
idempotent replay/conflict, internal-only dispatch, tenant/conversation binding,
latest checkpoint selection, retention and redaction. Follow the repository's
baseline → failing tests → implementation → focused/full suite → independent
review loop. Do not run two pytest processes concurrently.
