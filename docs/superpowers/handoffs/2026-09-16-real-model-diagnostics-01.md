# REAL-MODEL-DIAGNOSTICS-01 — Evidence-First Failure Classification

Date: 2026-09-16

Base: `odontoflow-backend/main@d6940c64cc0c8fe8ac88cdabd48e7435a8c4f86f`

Status: **IMPLEMENTED — the failure boundary is now observable; the prior real-model root cause remains UNKNOWN**

## TCADD requirement

The required outcome was evidence-based recovery, not a forced successful model
response. Known failures must be classified, unknown failures must stay
explicitly unknown, and diagnostics must not expose credentials, payloads, or
create retries or business mutations. This change satisfies that contract at
the existing Sales Agent runtime/API boundary without another provider request.

## Verified error path

The previous `HTTP 503` path was traced as:

1. `SalesAgentRuntime.turn()` loaded canonical inbound context through the
   authenticated typed gateway.
2. The runtime entered agent/model execution. A non-timeout exception was
   caught by the broad runtime boundary and replaced with a generic
   `SalesAgentExecutionError`.
3. `POST /sales-agent/turn` caught `RuntimeError` and returned the generic
   `AGENT_EXECUTION_FAILED` 503 envelope.
4. The running process exposed only the access-log status; no provider status,
   request ID, exception class, stage, or safe elapsed-time record was
   available.

The runtime now records the internal stage at `inbound_context`, `agent_build`,
`model_execution`, or `response_validation` before the same existing error
mapping occurs. The OpenAI Python SDK evidence used for classification was the
documented `APIStatusError`/status-specific subclasses, `APIConnectionError`,
and `APITimeoutError` hierarchy and its `status_code`/`request_id` fields.

## Root cause of the prior 503

**UNKNOWN.** Verified facts are that the caller did not time out, the configured
20-second provider deadline did not fire, the 180-second overall turn deadline
did not fire, and the canonical context tool completed before the 503. The
available evidence cannot distinguish provider rejection/availability, model
response/tool-calling incompatibility, or another runtime integration error.
No OpenRouter request was made during this activity to obtain new evidence.

## Diagnostic contract

The API keeps the existing generic public code/message. Failed turns now emit
one content-free warning record through the existing Python logging boundary:

```text
sales_agent_failure {"request_id":...,"correlation_id":...,"stage":...,"category":...,"elapsed_ms":...}
```

The JSON fields are allowlisted and bounded:

- `request_id` and `correlation_id`: validated UUIDs from the existing HTTP
  security middleware; the same safe IDs are returned in the error envelope's
  `details`.
- `stage`: `turn_deadline`, `inbound_context`, `agent_build`,
  `model_execution`, `response_validation`, or `request_boundary`.
- `category`: provider authentication, rate limit, invalid request, model
  unavailable, server, HTTP, connection, timeout, invalid response, turn
  timeout, gateway, runtime unavailable, invalid agent response, or `unknown`.
- `upstream_status` and `upstream_request_id`: included only when supplied by
  a recognized SDK/gateway error and sanitized.
- `elapsed_ms`: measured at the HTTP boundary.
- `partial_business_effects`: `false` when no mutating tool succeeded, `true`
  when a mutating tool returned success, and omitted when the existing evidence
  cannot establish the result.

Raw exception strings, provider response bodies, headers, authorization data,
credentials, patient messages, and stack traces are not serialized or logged.
No retry, fallback, outbound response, proposal duplication, or appointment
creation is introduced by diagnostics.

## Implementation

- `sales_agent/runtime.py`: added the content-free diagnostic value object and
  OpenAI-compatible exception classifier; preserved the runtime's existing
  typed exceptions, model controls, tool gateway, and telemetry shape. Added
  stage tracking and internal partial-effect observation.
- `sales_agent/api.py`: logs the sanitized diagnostic and adds only validated
  trace IDs to existing error details while preserving public error codes and
  messages.
- `tests/test_sales_agent_provider.py`: deterministic status, timeout, unknown,
  safe serialization, and no-tool-retry coverage.
- `tests/test_sales_agent_auth.py`: authenticated API boundary coverage for
  safe logging/serialization and one runtime attempt.
- `CHANGELOG.md`: recorded the bounded diagnostic contract.

No dependency, schema, provider, model, timeout, consent, booking, sandbox,
WhatsApp, n8n, production, or fallback change was made. The owner-supplied
environment and credentials were not read into output or modified.

## Verification

Commands and results:

- `./.venv/bin/python -m pytest -q tests/test_sales_agent_provider.py tests/test_sales_agent_auth.py tests/test_sales_agent_w3.py tests/test_sales_agent_w4.py tests/test_sandbox_inbound.py`
  — **53 passed, 2 warnings**.
- `./.venv/bin/ruff check sales_agent/runtime.py sales_agent/api.py tests/test_sales_agent_provider.py tests/test_sales_agent_auth.py`
  — **passed**.
- `git diff --check` — **passed**.
- `./.venv/bin/python -m pytest -q tests/test_idempotency.py::test_concurrent_different_keys_same_slot_settle_by_gist`
  — **1 passed, 2 warnings** in isolation.
- `./.venv/bin/python -m pytest -q` — **570 passed, 21 warnings** in
  `437.52s` (`7:17`). The previously reported concurrency test also passed in
  this complete run.

No OpenRouter, OpenAI, model, sandbox, or outbound request was made in this
activity.

## Concurrency and business-state status

The previous partial-turn state remains governed by the existing persistence
and idempotency contracts. Diagnostics only observe the runtime boundary; they
do not replay the earlier event, retry the mutating turn, alter committed
proposals, create appointments, persist outbound messages, or settle receipts.
Existing fake-model/W4 and sandbox sender tests continue to prove the
fail-closed and no-fabricated-outbound behavior.

## Blockers

The prior provider/runtime failure category is still unavailable without a
future real-model request. This activity intentionally did not make that
request. There is no diagnostic implementation blocker.

## Exactly one next activity

`SANDBOX-REAL-MODEL-04 — one newly authorized, bounded diagnostic smoke using
the existing OpenRouter model; stop on the first external failure and inspect
the new sanitized diagnostic record.` Do not replay the prior event, change
models, add fallback, or start it automatically.

Canonical Foreman brief:
`../odontoflow-planning/docs/handoffs/plans/2026-09-16-real-model-diagnostics-01.md`

Resulting commit: this handoff and implementation are included in the single
bounded commit reported by the completing agent.
