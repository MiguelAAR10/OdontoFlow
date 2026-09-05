# WF-01 Sales Agent V0

`workflows/WF-01-sales-agent-v0.json` is the inactive, repository-backed n8n
export for the smallest synthetic inbound loop. Its stable identity is `WF-01`
at version `0.1.0`; `meta.runtime_validation` records that no n8n runtime was
available when this artifact was created.

The workflow accepts only `provider=test`, normalizes an allowlisted text event,
buffers by provider/channel/contact for a bounded 250 ms window, persists the
inbound event, stops on the backend's `provider_message_id` duplicate receipt,
calls `POST /sales-agent/turn`, persists the reply through the authenticated
outbound endpoint, and returns a local test-provider result. The disabled
scheduled trigger is shape-only; it performs no autonomous follow-up.

Credentials are supplied at runtime through private n8n environment/credential
configuration (`ODONTOFLOW_BASE_URL`, `SALES_AGENT_BASE_URL`,
`ODONTOFLOW_INBOUND_TOKEN`, and `ODONTOFLOW_AGENT_TOKEN`). No token is embedded
in the export. The agent token must be provisioned with the `sales-agent-v0`
profile from `scripts/bootstrap_n8n_lab.py`.

The debounce store is intentionally process-local and bounded for this POC. A
restart loses buffered events, and concurrent n8n workers do not share a
distributed lock; replaying the normalized event is safe because the backend
deduplicates `provider_message_id`. The harness in
`wf_01_sales_agent_v0.py` exercises the same HTTP contract against injected
FastAPI clients and real PostgreSQL without pretending to validate or publish
the export through n8n.

Before any deployment, import the export into an actual n8n instance, validate
the graph, inspect its connections, run a side-effect-authorized synthetic test
with test credentials, and only then publish/version it. This repository does
not claim those lifecycle stages without an n8n runtime.
