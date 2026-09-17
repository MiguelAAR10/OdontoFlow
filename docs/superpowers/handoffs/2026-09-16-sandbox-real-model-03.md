# SANDBOX-REAL-MODEL-03 — Bounded Execution and Single Smoke

Date: 2026-09-16

Base: `main@18fae8deeccd423fe1d55d98144b5e3f4c0f2f76`

Status: **PARTIAL FAILURE — BOUNDED REAL-MODEL TURN RETURNED HTTP 503**

## Outcome

The runtime repair was implemented and verified without a provider request. A
single new synthetic sandbox event was then sent through the existing local
loopback path. The canonical inbound was accepted, but the one authenticated
`/sales-agent/turn` attempt returned HTTP 503. The sender did not retry the
mutating turn, did not persist an outbound, and the sandbox consumer was not
run. The earlier timed-out conversation and proposal were not replayed or
modified.

No model change, provider fallback, WhatsApp/n8n path, production endpoint,
automatic booking, or second paid request was used.

## Bounded configuration

The existing `langchain-openai` OpenAI-compatible adapter remains the only
provider integration. No additional dependency was required.

```text
provider=openrouter
model=deepseek/deepseek-v4-flash-0731
base_url=https://openrouter.ai/api/v1
key_source=OPENROUTER_API_KEY only
model_request_timeout_seconds=20.0
model_max_retries=0
model_max_output_tokens=512
sales_agent_turn_timeout_seconds=180.0
sales_agent_gateway_timeout_seconds=10.0
sales_agent_recursion_limit=12
sender_agent_turn_attempts=1
sandbox_consumer_request_timeout_seconds=10.0
single-smoke-sales-agent-client-timeout_seconds=195.0
```

The model request timeout is per provider request. The turn deadline is the
cooperative overall limit checked before and after gateway/model work and
inside the existing synchronous model/tool middleware. Existing finite gateway
HTTP timeouts bound tool calls. The caller used a 195-second timeout, above the
180-second server deadline; it did not fire.

The runtime passes `timeout`, `max_retries=0`, and `max_tokens` to
`init_chat_model` for both OpenRouter and native OpenAI. Provider, model, base
URL, and credentials remain server settings and are absent from the request
schema/configuration.

## Timeout trace and failure classification

The previous 15.278-second boundary was the old ad hoc caller timeout. In this
smoke the caller did not time out, and neither the configured 20-second model
request deadline nor the 180-second turn deadline fired. The Sales Agent API
returned HTTP 503 after approximately 7.5 seconds.

The first broken boundary is confirmed as the Sales Agent model-execution
boundary after the canonical inbound-context load. The exact external cause is
still uncertain: the API returned the existing content-free 503 envelope and
the running Uvicorn access log emitted no provider exception or content-free
telemetry record. No OpenRouter request ID, provider error category, token
usage, or cost was available. The evidence does not distinguish provider
rejection/availability, model-response/tool-calling incompatibility, and the
runtime integration error path.

The one successful gateway audit before the 503 was the runtime's required
`get_reception_context` load. No model-driven business tool was audited. The
failure was not a configured timeout.

## Single-smoke evidence

Sanitized preflight immediately before service start:

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

One new event used a fictitious contact and the existing `provider=sandbox`
channel. The event identity was not replayed. Counts and IDs after the first
failure:

```text
conversation_id=2 organization_id=1 channel_provider=sandbox
inbound_message_id=2 direction=inbound delivery_status=received
agent_turn_attempts=1
gateway_audit_id=10 organization_id=1 get_reception_context success duration_ms=38
model_driven_tool_calls=0
proposal_count_for_conversation=0
appointment_count_total=0
outbound_count_total=0
sandbox_receipt_count_total=0
sandbox_open_outbound_count=0
```

The prior real-smoke state remained unchanged:

```text
proposal_id=1 conversation_id=1 organization_id=1 status=pending appointment_id=None
```

The configured Sales Agent credential resolved through the existing
tenant-bound `sales-agent-v0` IAM profile for organization 1. All business
access observed in this attempt used the existing authenticated typed gateway.
The loopback database contains one organization, so this proves the configured
tenant binding but is not an independent second-tenant adversarial run.

No outbound response reached the canonical outbound command. Consequently,
there was no sandbox claim, receiver call, receipt, or settlement. The sender
made no mutating-turn retry and did not report successful delivery.

## Metrics

```text
new inbound HTTP requests=1 (201)
authenticated Sales Agent HTTP attempts=1 (503)
canonical outbound HTTP requests=0
sandbox claim/receive/settle requests=0
provider/model request result=not independently confirmed; runtime returned 503
model call count=not emitted by the failed process telemetry
tool call count=1 canonical context load; 0 model-driven business tools
input/output tokens=not reported
reported cost=not reported
caller-observed turn latency=approximately 7.5 seconds
```

## Changes and local environment

Changed files:

- `sales_agent/config.py`: explicit model request/turn/token settings.
- `sales_agent/runtime.py`: OpenAI-compatible bounded kwargs, cooperative
  deadline, provider/turn timeout classification, and deadline-safe handoff.
- `.env.example` and `scripts/bootstrap_openrouter_local.py`: safe defaults.
- `tests/test_sales_agent_provider.py` and `tests/test_sandbox_inbound.py`:
  focused provider, timeout, native OpenAI, fake-model, and no-fabricated-
  outbound proofs.
- `CHANGELOG.md`.

The ignored `.env.local` was updated only with the three non-secret bounded
settings. Existing owner-supplied values, including `OPENROUTER_API_KEY`, and
the existing tenant-bound `SALES_AGENT_V0_CREDENTIAL` were preserved. The
credential was resolved previously by the repository's
`scripts/bootstrap_openrouter_local.py` IAM issuance/preservation path; it was
not reissued, hardcoded, printed, or committed here.

Credential-free preflight command:

```text
./.venv/bin/python scripts/preflight_sales_agent.py
```

It makes no provider request. The local bootstrap command remains:

```text
./.venv/bin/python scripts/bootstrap_openrouter_local.py
```

It is idempotent and preserves existing secrets; it was not rerun during this
activity because the existing environment was already ready.

## Verification

- Focused provider/sender pack: **12 passed**.
- Runtime/fake-model/preflight/bootstrap focused pack after the final change:
  **37 passed**.
- `ruff check` on changed Python surfaces: passed.
- `git diff --check`: passed.
- No-network ChatOpenAI construction probe: `request_timeout=20.0`,
  `max_retries=0`, `max_tokens=512`; no provider request.
- Final full real-PostgreSQL suite: **565 passed, 1 unrelated concurrency
  failure, 21 warnings**. The failed existing
  `test_concurrent_different_keys_same_slot_settle_by_gist` returned the
  pre-existing “not a bookable slot” `AppError` instead of the race exception;
  the test passed in isolation (**1 passed**) immediately afterward. No
  changed file is in that scheduling/idempotency path.

## Exactly one recommended next activity

`SANDBOX-REAL-MODEL-04 — Expose a sanitized provider/runtime failure category`
at the existing Sales Agent telemetry/error boundary, preserving the current
provider/model, typed gateway, one-attempt mutation rule, and no-fallback
policy; only then decide whether one new smoke is warranted. Do not replay this
event or the earlier conversation automatically.

Commit: this handoff is included in the bounded changeset; the final commit
SHA is reported by the completing agent.
