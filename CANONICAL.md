# Canonical data boundary

OdontoFlow PostgreSQL is the only business authority. Integration actors use
the typed HTTP contracts and permission checks; agent prompts, model output and
external workflow state never set prices, availability, duration, bookings or
clinical decisions.

## Reception pilot boundary

- The prices in `Service.base_price`/`currency` and the five seeded offers in
  `scripts/seed_reception_demo.py` (including S/99, S/399, S/699, 15% and S/0)
  are **UNVERIFIED / NON-AUTHORITATIVE** synthetic pilot data. They are not
  clinic tariffs and are not exposed in Sales Agent reception context.
- No real OdontoSmart evidence, tariff source, promotion approval or clinic
  provenance is present in this repository. Do not make a commercial claim
  until the clinic confirms the source.
- `ContactIdentity.consent_status` is a declared data field, not a compliance
  claim or proof of lawful consent. Consent wiring remains a later capability.
- Reception capabilities for cancellation, rescheduling, operator resume,
  delivery management and unrelated contact-profile administration are
  dormant for `sales-agent-v0`; the credential profile denies their
  permissions. Promotion exposure and upselling are also out of scope.

The deferred promotion, pricing and proposal tables remain physically present
because the migration chain is immutable. Their presence is not evidence that
the Sales Agent may read or act on them.
