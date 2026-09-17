# SANDBOX-REAL-MODEL-04 — One Diagnostic Smoke

Date: 2026-09-16
Base: `db5278954f11e4a8870051637d2046808133f927`
Status: **BLOCKED BEFORE SALES AGENT**

## Result

Exactly one new synthetic sandbox booking-intent event was sent through the
existing `SandboxInboundSender`. Its first canonical request to
`POST /internal/messages/inbound` returned HTTP 404, and the sender stopped
with `SandboxSenderTransportError`. The configured Sales Agent attempt budget
was one, but the Sales Agent was never reached. No OpenRouter request, model
execution, tool call, outbound message, sandbox consumer action, or receipt
was produced. No retry, fallback, replay, or repair was performed.

The first observed broken boundary is **sandbox sender → canonical backend
inbound HTTP boundary**. This is not evidence of a provider or Sales Agent
failure.

## Precheck

The reported backend HEAD matched. The latest diagnostics handoff was present.
The credential-free preflight reported:

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

PostgreSQL was reachable on the configured local port, the canonical backend
health endpoint returned HTTP 200, and the Sales Agent loopback service was
reachable. Preserved bounds were provider timeout 20s, `max_retries=0`,
`max_output_tokens=512`, overall turn timeout 180s, and caller timeout 195s.

## Diagnostic evidence

No `sales_agent.diagnostics` record was emitted because no Sales Agent turn
started:

```text
diagnostic_stage=not_emitted
diagnostic_category=not_emitted
upstream_status=404
request_id=not_available
correlation_id=not_available
elapsed_ms=not_available
partial_business_effects=false
```

The canonical backend access log recorded the actual smoke request as
`POST /internal/messages/inbound ... 404 Not Found`. A later direct,
unauthenticated non-event probe to the same route returned 401; source
inspection also shows the route is registered at `/internal/messages/inbound`.
Those facts confirm the first observed status but do not establish why the
sender's effective request received 404. The underlying ingress discrepancy
remains unresolved. No provider/runtime classification is inferred.

## Database evidence

The read-only post-smoke census found no row for the new event and no new
business effect:

```text
sandbox_conversations_total=2
sandbox_conversation_id=1 organization_id=1 status=awaiting_confirmation provider=sandbox
sandbox_conversation_id=2 organization_id=1 status=open provider=sandbox
sandbox_inbound_total=2
sandbox_inbound_message_id=1 conversation_id=1 delivery_status=received
sandbox_inbound_message_id=2 conversation_id=2 delivery_status=received
new_smoke_persisted_rows=0
new_smoke_tool_audits=0
proposals_total=1
proposal_id=1 organization_id=1 status=pending appointment_id=None
appointments_total=0
outbounds_total=0
sandbox_receipts_total=0
automatic_appointments_created=0
```

The earlier pending proposal was not modified. Tenant evidence for the
existing rows remained organization 1.

## Provider, tools, and delivery

Configured provider/model: `openrouter /
deepseek/deepseek-v4-flash-0731`. Actual provider requests: 0. Actual model
requests: 0. Sales Agent turn attempts: 0. Tool calls: 0. Provider tokens,
cost, request ID, and model latency: unavailable because execution never
reached the provider. No credits were consumed. No outbound was persisted, so
the sandbox consumer was not invoked and no receipt exists.

## Verification and code state

The preflight, PostgreSQL reachability, canonical health check, and loopback
service checks passed. The one event stopped at the first 404. Both local
services were stopped by their exact process IDs after evidence collection.
No code or runtime configuration changed. No pytest run was required. The
only new repository artifact is this handoff; no credentials, event payloads,
contact details, or secret values are included.

## Exact blocker and next activity

The next activity must resolve and verify why the sender's effective backend
origin/request returned 404 while the live route later returned 401, then prove
authenticated canonical inbound persistence without invoking Sales Agent or
OpenRouter. Recommended single activity:

`SANDBOX-INGRESS-ROUTE-01 — resolve and verify canonical inbound route/auth boundary`

Do not start it automatically. Do not replay this event or modify the prior
pending proposal.

Planning brief:
`../odontoflow-planning/docs/handoffs/plans/2026-09-16-sandbox-real-model-04.md`
