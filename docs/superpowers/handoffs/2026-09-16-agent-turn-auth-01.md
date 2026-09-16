# AGENT-TURN-AUTH-01 — Technical handoff

Date: 2026-09-16

Base: `021fa5297f520313f4920d6916e069f09fb640dc`

Status: **PASS — Sales Agent entrypoint secured; no next activity started**

## Result

`POST /sales-agent/turn` now rejects the request before the Sales Agent runtime
is reached unless all of the following are true:

- the existing PostgreSQL-backed `require_authenticated_context` resolves the
  bearer credential to a live principal and tenant;
- the credential is the server-configured credential for this Sales Agent
  process (and, for an injected runtime, agrees with its configured gateway
  credential); and
- the resolved principal is an `agent` with the existing `conversations.read`
  permission.

Missing, invalid, cross-tenant, unconfigured, or mismatched credentials fail
closed with the existing authentication envelope. Valid non-agent principals
and agents without the entrypoint permission receive the existing permission
denial envelope. No model execution, conversation mutation, or agent tool call
occurs on those paths. Identity, principal type, permission, and organization
come from PostgreSQL-backed server context; no body field, LLM assertion,
caller trust flag, hardcoded token, or production bypass was added.

The standalone Sales Agent app now uses the existing error handlers, transport
security middleware, bearer scheme, and security OpenAPI decoration. The
canonical backend tool gateway and its per-tool permission checks remain
unchanged. The WF-01 runner and checked-in n8n export forward the existing
`ODONTOFLOW_AGENT_TOKEN` to the turn endpoint. That token must be the same
server-issued `sales-agent-v0` credential configured as
`SALES_AGENT_V0_CREDENTIAL`; both remain runtime configuration, not committed
secrets.

## Tests and verification

The new real-PostgreSQL auth regression was intentionally red before the route
change: the five boundary assertions that required auth/authorization failed
against the previously open route. After implementation, the focused auth,
Sales Agent W3/W4, reception, authentication, and security-boundary run was:

```text
84 passed, 2 warnings in 59.74s
```

The serial full suite was:

```text
535 passed, 21 warnings in 405.49s (0:06:45)
```

The current collection is **535**, compared with the historical **527-pass**
baseline and the preceding **529-test** state. The six new tests cover missing
credentials, invalid credentials, valid non-agent principal, cross-tenant
agent credential, missing permission, authorized invocation, and bearer
OpenAPI publication. Existing W4 proposal, temporal/fail-closed booking,
isolation, idempotency, expiry, and audit tests remain green.

Additional checks passed:

- `git diff --check`;
- JSON parsing of `WF-01-sales-agent-v0.json`;
- targeted Ruff checks for the new route and auth test file.

## Files in the bounded change

- `sales_agent/api.py` — existing server authentication/context gate and
  configured service-identity/permission authorization;
- `integrations/n8n/wf_01_sales_agent_v0.py` — turn-call bearer forwarding;
- `integrations/n8n/workflows/WF-01-sales-agent-v0.json` — exported turn-call
  bearer header;
- `integrations/n8n/README.md` — credential-binding contract;
- `tests/test_sales_agent_auth.py` — real-PostgreSQL entrypoint regressions;
- `tests/test_sales_agent_w3.py` and `tests/test_sales_agent_w4.py` — anonymous
  rejection, caller wiring, and export/runtime coverage;
- `CHANGELOG.md` — shipped change record;
- this handoff.

Pre-existing modified/untracked paths (`.gitignore`, `AGENTS.md`, skill and
tooling directories, architecture/review material, and prior handoffs) were
not staged or changed by this activity.

## Invariants and limitations

The temporal and MVP fail-closed booking guards remain intact: agent-driven
confirmation still cannot turn unverified free text into an appointment.
Tenant/conversation isolation, tool permissions, audit behavior, proposal
expiry, idempotency, and exactly-one appointment semantics were not relaxed.
No migration, schema, channel, human login, provider billing, deployment, or
production data was touched.

The local fake-model flow remains injectable only through a server-configured
credential and the real PostgreSQL auth maker; passing an injected runtime does
not skip the gate. The checked-in n8n export remains inactive and no live n8n
runtime or channel was claimed.

## Commit

The implementation, tests, changelog, and this handoff are included in one
bounded task commit. The final SHA is recorded in the planning CAVELOG and
STATUS records and in the task close-out response.

Planning living brief:
`../odontoflow-planning/docs/handoffs/plans/2026-09-16-agent-turn-auth-01.md`

Do not start another activity automatically.
