# W4 Handoff — WF-01 synthetic Sales Agent loop

Date: 2026-09-05
Workstream: W4 — n8n WF-01 wiring against `provider=test`
Base: `159cbe23769f0161d2cf4084d0020e4c30e160ed`
Status: **NEEDS_PLANNING** for the actual n8n lifecycle gate; the repository-backed
contract harness and real PostgreSQL/HTTP synthetic loop are implemented and
passing.

## Objective and boundary

This workstream implements the smallest repository-backed representation of:

```text
synthetic/test inbound
  → normalization
  → bounded conversation-scoped debounce
  → POST /internal/messages/inbound
  → POST /sales-agent/turn
  → seven-tool W3 runtime gateway
  → POST /internal/conversations/{conversation_id}/outbound
  → local test-provider result
```

The checked-in export is inactive and test-only. It contains no live clinic,
patient, WhatsApp, provider, model, revenue, promotion, or pricing claim.

## Workflow identity and runtime evidence

The repository artifact is:

```text
integrations/n8n/workflows/WF-01-sales-agent-v0.json
workflow key: WF-01
artifact version: 0.1.0
meta.status: repository-export-only
meta.runtime_validation: unavailable-no-n8n-runtime
active: false
```

Fresh environment evidence:

```text
$ command -v n8n
[no output]
$ docker ps --format '{{.Names}}\t{{.Image}}'
odontoflow-db-1    postgres:15-alpine
```

The available tool surface also contained no n8n workflow tools. Consequently
there is no honest n8n `validate_workflow`, `get_workflow_details`,
`test_workflow`, `publish_workflow`, workflow ID, or published version to
report. The next owner must run the official lifecycle in an actual n8n
instance before changing `active` or claiming W4 PASS.

## Observable synthetic E2E

The Python harness in
`integrations/n8n/wf_01_sales_agent_v0.py` has no database or ORM access. It
only normalizes an allowlisted event, stores a bounded process-local debounce
buffer, performs authenticated HTTP calls through injected clients, and
returns a clearly marked local test-provider result. The test uses the real
FastAPI backend, real PostgreSQL canonical database, a separate scratch
PostgreSQL agent-memory database with W3 `PostgresSaver`, and a deterministic
fake model; no live model/provider call is made.

Command and fresh output:

```text
$ .venv/bin/python -m pytest -q tests/test_sales_agent_w4.py tests/test_sales_agent_w3.py tests/test_bootstrap_n8n_lab.py tests/test_messaging_phase2.py
40 passed, 1 warning in 54.15s
```

The W4 tests prove:

- first inbound events are `201` receipts and the same `provider_message_id`
  returns a `200` duplicate receipt without a second agent turn or outbound;
- three independent `WF01Runner` executions for one contact carry the returned
  `conversation_id` into W3 `/sales-agent/turn`, and the separate checkpointer
  resumes the same thread;
- two contacts receive different canonical conversations and their W3 message
  histories contain only their own three inbound texts;
- the three-turn scenario is request cleaning → `Lince`/Monday time → explicit
  `Yes confirm`;
- turn one calls canonical `list_services` and returns `continue`;
- turn two calls canonical `list_locations`, then canonical
  `query_available_slots`, then `propose_appointment` and returns `proposed`;
- the appointment count is asserted as zero after turn two, so no booking
  occurs before confirmation;
- turn three reads canonical `pending_action`, calls `confirm_appointment`,
  returns `confirmed`, and persists the reply through the canonical outbound
  endpoint;
- a fourth synthetic inbound with a new provider message ID repeating the
  confirmation leaves the canonical appointment count unchanged at one;
- the selected appointment starts exactly at the slot returned by the
  canonical availability tool;
- replaying the third event with the same provider ID leaves exactly one
  appointment for that conversation;
- test-provider output is local `synthetic_only` with canonical outbound status
  `pending`; no external claim or delivery is attempted;
- all observed agent tool calls are within the seven W3 names and cancellation/
  rescheduling names never occur;
- canonical reception context contains neither a `promotions` key nor price
  data; and
- a persisted human handoff returns a typed `ENTITY_INACTIVE` tool error for a
  subsequent Sales Agent read, preventing automation.

## Exact HTTP contracts used

| Step | Request and owner | Contract evidence |
|---|---|---|
| Ingress | Authenticated `POST /internal/messages/inbound` using `ODONTOFLOW_INBOUND_TOKEN` | Body is W3 schema `1.0`, provider `test`, channel `odonto-smart-lab`, provider message ID, contact ID, E.164 phone, text, and timezone-aware `occurred_at`; transport UUIDv4 `Idempotency-Key` plus matching trace headers. |
| Dedupe | OdontoFlow service | `provider_message_id` is the durable backend key; n8n/harness does not replace it with an in-memory business decision. |
| Agent turn | `POST /sales-agent/turn` with `conversation_id` and `latest_inbound_message_id` | Exact W3 request/response contract; the export intentionally does not retry this non-idempotent runtime call. |
| Tools | Sales Agent → authenticated `POST /agent-tools/call` | W3 `BackendGateway` sends UUID request/correlation IDs, read version `1.0` with null idempotency, and mutation version `1.1` with UUIDv4 keys. Only the seven approved wrappers are built. |
| Outbound transcript | Authenticated `POST /internal/conversations/{conversation_id}/outbound` using `ODONTOFLOW_AGENT_TOKEN` | Fresh UUIDv4 key is generated once per logical outbound intent and reused for bounded transport retries; the backend stores the message and queue row atomically. |
| Test result | Local harness/export Code node | `provider=test`, `delivery_mode=synthetic_only`; test outbounds remain pending and are intentionally not sent or claimed by a real provider. |

Inbound and outbound HTTP nodes have bounded transport retry (`maxTries=3`).
The Sales Agent turn has retry disabled because W3 has no turn idempotency
contract; repeating a model turn could create a new proposal. Business
idempotency remains in the backend commands.

## Workflow ownership and safety

The export gives n8n only trigger, input normalization, bounded buffering,
HTTP orchestration, transport retry, a disabled scheduled-follow-up shape, and
presentation of a local test result. There is no database node, SQL, catalog
lookup, availability calculation, booking decision, pricing/promotion input,
agent-memory store, or external provider delivery node.

The seven-tool credential is now provisioned by
`scripts/bootstrap_n8n_lab.py` with the exact `sales-agent-v0` permission
profile: `conversations.read`, `services.read`, `locations.read`,
`availability.read`, `contact_appointments.read`,
`contact_appointments.book`, `conversations.manage`, and
`deliveries.create`. It does not receive cancellation, rescheduling, profile,
practitioner, resume, or delivery-management permissions.

## Debounce and restart/concurrency limits

The export uses a 250 ms process-local buffer keyed by
`provider/channel_account_external_id/external_contact_id`. The canonical
conversation ID is returned only after ingress, so this stable contact stream
key is the safe pre-ingress scope for an open conversation. The buffer is
bounded at eight events and fails closed when full; it never silently drops an
event.

Because this is a POC, a process restart loses buffered events and multiple n8n
workers do not share a distributed lock. Replayed events remain safe because
the backend `provider_message_id` constraint is authoritative. No Redis or
other horizontal state was added because there is no measured requirement.

## Files changed

- `integrations/__init__.py`
- `integrations/n8n/__init__.py`
- `integrations/n8n/README.md`
- `integrations/n8n/wf_01_sales_agent_v0.py`
- `integrations/n8n/workflows/WF-01-sales-agent-v0.json`
- `scripts/bootstrap_n8n_lab.py` — agent lab token now uses `sales-agent-v0`
- `tests/test_sales_agent_w4.py`
- `CHANGELOG.md`
- this handoff

No Alembic revision, canonical application module, scheduling authority,
MediStock file, W3 runtime contract, or planning/control-plane file was
changed. The pre-existing dirty `AGENTS.md`, `.agents/`, `.claude/`,
`.playwright-mcp/`, `docs/integration/TEAM_SYNC_AND_BRAND_TRANSFER.md`, and
`skills-lock.json` files were preserved and are not part of W4.

## Verification and remaining gate

Additional checks run before handoff:

```text
$ .venv/bin/python -m json.tool integrations/n8n/workflows/WF-01-sales-agent-v0.json >/dev/null
JSON_VALID
$ .venv/bin/python -m compileall -q integrations tests/test_sales_agent_w4.py
COMPILE_PASS
$ git diff --check
DIFF_CHECK_PASS
```

Final full PostgreSQL suite evidence, run after the handoff was staged and
before completion:

```text
$ .venv/bin/python -m pytest -q
502 passed, 20 warnings in 654.23s (0:10:54)
```

W3's existing optional-import guard remains green in the focused run; W4's
real-agent E2E is skipped automatically when the optional LangChain/LangGraph
group is absent.

## Recommended next activity

Provision or register an isolated n8n test instance, import the inactive
export, verify credentials node-by-node, run `validate_workflow`, inspect
connections with `get_workflow_details`, and run a side-effect-authorized
synthetic test using only the test channel and fake/stub model. Record the n8n
workflow ID and published version only after those checks pass; keep this
artifact inactive and report W4 as NEEDS_PLANNING until then.
