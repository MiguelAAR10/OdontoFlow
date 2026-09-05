# Handoff — n8n reception hardening

## Outcome

The clean GitHub-derived backend now exposes a synthetic reception boundary
that can be connected to n8n without pretending to be WhatsApp.

## Shipped behavior

- Migration `0015` admits `ChannelAccount.provider=test`, adds the operator-only
  `conversations.resume` permission and persists cancellation proposals.
- Test-provider messages are durable, while their outbound queue rows are never
  claimable by an external dispatcher.
- Every LLM-facing read or mutation is blocked while a conversation is in
  `human_handoff`.
- `resume_automation` was removed from the agent-tool enum. The authenticated
  operator route is `POST /internal/conversations/{conversation_id}/resume`.
- Cancellation is now proposal/confirmation and the confirmation must reference
  a different, later inbound message from the same conversation.
- `scripts/bootstrap_n8n_lab.py` seeds the synthetic clinic, provisions the test
  channel and rotates three separate least-privilege credentials without
  printing their tokens.
- OpenAPI was regenerated and the n8n reception contract checker was aligned to
  the safe tool names.

## Local runtime prepared

- Dedicated `odontoflow_n8n_lab` database created from empty, upgraded to
  Alembic `0015`; `alembic check` reports no pending operations. Existing
  development data was not deleted or reused.
- Synthetic ODONTO SMART catalog loaded.
- Test channel `odonto-smart-lab` provisioned as channel account id `1`.
- Local credentials stored in ignored `.env.n8n.local`.
- Authenticated HTTP smoke: inbound message accepted, reception context returned
  25 services and exactly 3 synthetic locations.

## Verification

- Focused migration, messaging and reception suite: `30 passed`.
- Bootstrap/profile tests: `2 passed`.
- Full suite: `466 passed`, `21 warnings`, `0 failures` in `686.78s`.

## Boundary for the next task

The n8n package in `recepcion-odonto-smart` now calls the public API instead of
the mock broker. `get_reception_context` was extended for the pilot so every
new turn can recover the current conversation, contact profile, eight recent
messages, contact-bound confirmed appointments and the latest valid pending
proposal. This state is sourced from PostgreSQL through OdontoFlow only; n8n
never connects to the database.

The supervised test-channel path has live evidence for information, slot
search, profile registration, proposal creation and booking confirmation. The
remaining production boundary is still the external channel: Telegram and
WhatsApp remain disabled, and a temporary Cloudflare quick tunnel is not a
stable pilot ingress.
