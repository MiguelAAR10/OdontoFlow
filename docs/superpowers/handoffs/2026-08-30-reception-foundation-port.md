# Handoff — reception foundation port

Date: 2026-08-30  
Branch: `codex/reception-pilot`  
Base: GitHub `main` at `23527c21e1d83bbcc39cbc029bb740b2fd7e1312`

## Objective

Recover the useful security, messaging and deterministic reception work from
the local `OdontoFlow-Backend-security` working tree without continuing to
develop on its 69 mixed pending paths.

## Method

1. Cherry-picked the committed authentication task (`0fc7ed2`) onto clean
   GitHub main; resulting commit `1f2d8c1` passed 52 focused and 403 full tests.
2. Added the phase 1–5 tests before their implementation. The focused pack
   failed during collection on the expected missing modules and models.
3. Ported only runtime modules, migrations and operational scripts. Excluded
   old OpenAPI snapshots, binary deliverables, simulators and unrelated docs.
4. Ran the focused reception/security pack: 55 passed.

## Surfaces added

- `alembic/versions/0010_*` through `0014_*`
- `app/http_security.py`
- `app/messaging/`
- `app/agent_tools/`
- `app/run.py`
- credential, channel provisioning, redaction, demo seed and OpenAPI scripts
- model/service changes required by contact-bound reception

## Preserved invariants

- PostgreSQL remains the final authority.
- Scheduling duration and overlap constraints are unchanged.
- Mutations remain idempotent and transaction-owned by services.
- Business routes remain bearer-authenticated.
- No Redis, LLM SDK or external channel client was introduced.

## Known blockers carried into hardening

- Inbound provider is still restricted to `whatsapp`.
- `resume_automation` is still callable by the conversation-agent profile.
- Agent tools are not globally blocked while a conversation is in
  `human_handoff`.
- Cancellation still accepts a confirmation literal in one tool call rather
  than consuming a persisted proposal created by an earlier message.
- OpenAPI has not been regenerated on this branch yet.

The n8n readiness gate remains closed until those blockers have tests and are
implemented.
