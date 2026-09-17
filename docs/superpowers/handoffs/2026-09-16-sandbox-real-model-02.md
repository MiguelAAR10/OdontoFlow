# SANDBOX-REAL-MODEL-02 — First Real LLM End-to-End Smoke

Date: 2026-09-16

Smoke base: `main@1d4a710b15299c0ddb2317a230cb4a2fdd806052`

Status: **PARTIAL FAILURE — PROVIDER EXECUTION REACHED TOOLS, SALES AGENT
TURN TIMED OUT**

## Result

Exactly one new synthetic booking-intent inbound event was sent through the
local loopback path. No replay, model change, provider fallback, consumer
retry, or second paid request was started after the first failure.

The configured OpenRouter execution reached the typed tool gateway: all six
observed gateway calls returned HTTP 200 and the canonical booking boundary
created one pending proposal. The authenticated `/sales-agent/turn` response
did not return to the sender within its 15-second smoke-client timeout. The
provider connection was still open when the Sales Agent process was stopped.
No provider HTTP error body, completed Sales Agent telemetry record, token
usage record, or cost record was available.

The first broken boundary is the Sales Agent/provider runtime timeout contract:
the existing adapter has no explicit provider timeout, output-token cap, or
zero-retry setting. The sender correctly does not retry the mutating agent
turn, but the provider client currently exposes its library default retry
setting. This is a targeted runtime-bounding repair for the next activity,
not a reason to redesign the agent or change providers.

## Provider and model

- Provider: `openrouter`
- Model: `deepseek/deepseek-v4-flash-0731`
- OpenAI-compatible adapter: existing `langchain-openai` through
  `langchain.chat_models.init_chat_model`
- OpenRouter base URL: `https://openrouter.ai/api/v1`
- OpenRouter key source: `OPENROUTER_API_KEY` only; the value is not recorded
- No `langchain-openrouter` or other dependency was added
- No caller-controlled provider, model, base URL, or credential path was used

The exact server-side environment names in scope were
`SALES_AGENT_MODEL_PROVIDER`, `SALES_AGENT_MODEL`,
`SALES_AGENT_MODEL_BASE_URL`, `OPENROUTER_API_KEY`,
`SALES_AGENT_V0_CREDENTIAL`, `SALES_AGENT_DATABASE_URL`,
`SALES_AGENT_BACKEND_URL`, `SANDBOX_BACKEND_URL`,
`SANDBOX_RECEIVER_URL`, `SANDBOX_INBOUND_TOKEN`, and
`SANDBOX_DISPATCHER_TOKEN`. The Sales Agent credential was the existing
tenant-bound `sales-agent-v0` IAM credential from the prior bootstrap; it was
not reissued or printed during this smoke.

The tool results prove that OpenRouter model output was accepted far enough to
execute the observed tool calls. The final model/structured-response boundary
did not complete before the smoke client timed out, so this is not a PASS for
the complete conversation.

## Sanitized preflight and local services

Before the smoke:

```text
provider=openrouter
model=deepseek/deepseek-v4-flash-0731
base_url_configured=true
openrouter_api_key_configured=true
sales_agent_credential_configured=true
postgres_configured=true
sandbox_configured=true
ready_for_real_model_smoke=true
```

PostgreSQL was the healthy loopback `odontoflow-db-1` container on port 5434.
The canonical API ran on `127.0.0.1:8000`; the Sales Agent API ran on
`127.0.0.1:8001`. Both were stopped after the smoke. The local environment
and service credentials were read from ignored `.env.local`; no value was
printed, copied into this handoff, or committed.

## Runtime controls observed

- Sales Agent recursion limit: `12`
- Sales Agent gateway/request setting: `10.0` seconds
- Sender agent-turn attempts: `1` (the existing mutating-turn contract)
- Sender canonical inbound/outbound transport attempts: up to `3`, using the
  existing idempotency keys; neither leg was retried in this run
- One-shot smoke HTTP client timeout: `15.0` seconds
- Observed sender failure latency: `15,278 ms`
- Safe execution was finite by event count and recursion limit, but the model
  invocation had no explicit output-token cap or provider timeout
- A no-network model construction probe showed the existing OpenAI client
  defaults `max_retries=2`, `timeout=None`, and `max_tokens=None` for the
  current runtime kwargs. No new retry or fallback was introduced.

## Tool evidence

Every observed tool request used the authenticated `/agent-tools/call` gateway;
no direct database/model-side business access was used. Sanitized audit rows
for conversation `1` were:

```text
audit_id=2 organization_id=1 get_reception_context success 72ms
audit_id=3 organization_id=1 list_services success 24ms
audit_id=4 organization_id=1 list_locations success 27ms
audit_id=5 organization_id=1 get_reception_context success 59ms
audit_id=6 organization_id=1 query_available_slots success 57ms
audit_id=8 organization_id=1 propose_appointment success 147ms
```

The first `get_reception_context` is the runtime's inbound-context load; the
remaining calls are the model-driven tool sequence. The proposal creation
audit was `audit_id=7`, `appointment_proposal.created`, organization `1`.
No confirmation tool call occurred.

The authenticated service credential resolves to organization `1` and the
observed tool audit rows are all organization `1`. This local database had one
organization, so the smoke proves the tenant-bound runtime path and observed
tenant scope but is not an independent second-tenant adversarial exercise.

## Database evidence

Read-only baseline immediately before the event:

```text
organizations=1
sandbox_channels=1
sandbox_pending_outbound=0
conversations=0
appointments=0
pending_appointment_proposals=0
agent_credentials_org1=1
```

After the failed turn:

```text
conversation_id=1 organization_id=1 channel_account_id=1 contact_identity_id=1
message_count=1 message_ids=1
message_directions=1:inbound:received
agent_tool_audit_count=6
proposal_id=1 organization_id=1 status=pending appointment_id=None
conversation_status=awaiting_confirmation
proposal_service_id=3 proposal_location_id=1 proposal_practitioner_id=1
proposal_start_utc=2026-09-22T14:00:00+00:00
proposal_end_utc=2026-09-22T15:00:00+00:00
outbound_count=0
appointments_org1=0
pending_proposals_org1=1
```

The channel provider for the conversation was `sandbox`. No confirmed
appointment was created, and the proposal retained no appointment ID.

## Outbound receipt and settlement

No canonical outbound command was reached because the Sales Agent HTTP turn
did not return a reply. Consequently:

- outbound message rows: `0`
- sandbox receipt rows: `0` from this run
- settlement calls: `0`
- `SandboxConsumer`: not run, by the first-error stop rule
- live provider outbound delivery: `0`; no WhatsApp or other provider path was
  touched

The existing sender still has the required agent-turn no-retry behavior, and
the existing consumer remains the only sandbox claim/receive/settle path. The
real smoke did not reach that path, so delivery/settlement is unproven in this
activity rather than reported as successful.

## Request count, tokens, cost, latency

```text
canonical inbound HTTP requests: 1 (201)
authenticated Sales Agent HTTP attempts: 1 (no response before timeout)
canonical outbound HTTP requests: 0
sandbox consumer claim/receive/settle requests: 0
typed gateway calls: 6 (all 200)
provider model-call count: not reported; the turn was canceled before the
  content-free telemetry event was emitted
input/output tokens: not reported
OpenRouter reported cost: not available from the canceled runtime response
smoke latency to first broken boundary: 15,278 ms
```

The provider request may have incurred provider-side cost; this run has no
authoritative cost field to report. No further paid request was made to chase
the result.

## Verification and tests

Commands/evidence used:

- `git rev-parse HEAD` → `1d4a710b15299c0ddb2317a230cb4a2fdd806052` before the
  evidence handoff was added.
- `./.venv/bin/python scripts/preflight_sales_agent.py` → the sanitized READY
  output above; this made no network request.
- Docker health and loopback API checks → PostgreSQL healthy, canonical
  `/health` 200, Sales Agent `/openapi.json` 200 before the smoke.
- Read-only PostgreSQL queries → baseline, tool audit, proposal, appointment,
  and outbound evidence above.
- No pytest run was needed: no product/runtime code, schema, dependency, or
  test file changed in this activity. The prior OpenRouter bootstrap commit's
  focused and full-suite baselines remain the applicable code baseline.

No secret values, inbound text, phone number, model response, auth header,
provider response body, or credential was printed or persisted here.

## Exactly one recommended next activity

`SANDBOX-REAL-MODEL-03 — Bound the existing OpenRouter execution`: add the
smallest tested runtime configuration for an explicit provider timeout,
`max_retries=0`, and a finite output-token cap while preserving native OpenAI
support and the current provider-independent agent/gateway/tool boundaries;
then run one new smoke only after that change is reviewed. Do not start it
automatically.
