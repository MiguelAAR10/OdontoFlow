# OdontoFlow Lead-to-Appointment Vertical

## Status

Approved in conversation on 2026-08-12. This document defines the first implementation vertical; it does not authorize implementation by itself.

## Goal

Deliver the smallest operational OdontoFlow flow that converts a commercial lead into a confirmed multi-location appointment. OdontoFlow, not an LLM or external calendar, is authoritative for service duration, practitioner capability, availability, and booking conflicts.

## Scope

Included:

- Register a lead and its acquisition source: promotion, referral, or direct.
- Record the lead's service need.
- Maintain active services and their durations.
- Maintain locations and their time zones.
- Maintain practitioners and their service/location capabilities.
- Maintain recurring internal availability and exceptional schedule blocks.
- Find eligible appointment slots.
- Confirm, cancel, and reschedule appointments.
- Audit appointment creation, cancellation, and rescheduling.

Excluded from this vertical:

- Clinical records and visit outcomes.
- Charges, payments, debts, and NubeFact.
- Inventory and product consumption.
- WhatsApp, Google Calendar, voice, and STT adapters.
- Autonomous agents or LLM decisions in deterministic business rules.
- MediStock table-for-table migration.

## Architecture

Use one FastAPI deployment backed by PostgreSQL. Organize it as a modular monolith with explicit internal boundaries:

- `commercial`: leads, acquisition sources, service needs, and commercial status.
- `catalog`: active odontological services and authoritative duration.
- `organization`: locations, practitioners, and practitioner capabilities.
- `scheduling`: availability rules, exceptional blocks, appointments, and booking use cases.
- `audit`: append-only records for relevant state transitions.

Modules expose application use cases rather than sharing route-layer logic. Infrastructure adapters implement persistence and, later, external integrations. External systems may synchronize or request actions but never become domain authorities.

## Conceptual Model

### Lead

- Full name and at least one contact channel: phone or email.
- Acquisition source: `promotion`, `referral`, or `direct`.
- Service need.
- Commercial status.
- Lead deduplication is out of scope for this vertical.

### Service

- Stable identifier, name, authoritative duration, and active state.
- Client input cannot override duration during booking.

### Location

- Stable identifier, name, active state, and IANA time zone.
- Every appointment belongs to one location.

### Practitioner

- Stable identifier, display name, and active state.

### PractitionerCapability

- Associates a practitioner with a service and location.
- Only active capabilities make a practitioner eligible for booking.

### AvailabilityRule

- Recurring working interval for a practitioner at a location.
- Stored and evaluated in the location's time zone.

### ScheduleBlock

- Exceptional closed interval for a practitioner at a location.
- Represents absence, manual hold, or another non-bookable interval.

### Appointment

- References lead, service, practitioner, and location.
- Stores start and end instants plus state.
- States in this vertical: `confirmed` and `cancelled`.
- `completed` and `no_show` are reserved for the later clinical-transition vertical.
- State transitions and rescheduling are audited.

## Core Flow

1. Register or select a lead.
2. Associate the lead with an active service need.
3. Query slots for service, location, and date range.
4. Filter practitioners by active practitioner, active capability, service, and location.
5. Compute candidate starts on a 15-minute grid in the location's time zone. Use catalog duration and retain only intervals that fit wholly within recurring availability and intersect neither schedule blocks nor existing confirmed appointments.
6. Select a slot.
7. In one transaction, reload authoritative service duration, revalidate capability and availability, reject overlaps, and create a confirmed appointment.
8. Return the appointment with stable identifiers and calculated start/end values.

Cancellation marks an appointment cancelled and releases its time interval. Rescheduling validates the replacement interval and updates the same reservation atomically. It writes one audit record in the same transaction, with the old and new intervals in its before/after payload; no intermediate reservation state is externally visible.

## Deterministic Rules

- Service, location, practitioner, and practitioner capability must be active.
- Practitioner capability must match the selected service and location.
- Appointment time must fit wholly within an availability interval.
- Appointment time must not intersect a schedule block.
- A practitioner cannot have overlapping active appointments.
- Service duration comes from the catalog.
- Booking and rescheduling must be concurrency-safe.
- Only confirmed appointments consume availability; cancelled appointments do not.
- Application code returns deterministic conflict responses; an LLM cannot override them.

The database must provide the final concurrency guarantee against overlapping active appointments. The application performs preflight validation for clear errors, but correctness cannot depend on a race-prone read-before-write check alone.

PostgreSQL enforces this with a partial GiST exclusion constraint equivalent to `EXCLUDE USING gist (practitioner_id WITH =, tstzrange(start_utc, end_utc, '[)') WITH &&) WHERE (state = 'confirmed')`. The required extension is enabled by migration. SQLSTATE `23P01` maps to the stable appointment-conflict `409` response. Cancelled appointments therefore do not block interval reuse.

## API Behavior

Initial application surfaces:

- Register and read leads.
- Create/read active services, locations, practitioners, and capabilities through administrative surfaces needed by tests and local operation.
- Define availability and schedule blocks.
- Query eligible slots.
- Confirm an appointment.
- Cancel an appointment.
- Reschedule an appointment.

Error contract:

- `422` for malformed or structurally invalid input.
- `404` for referenced concepts that do not exist.
- `409` for inactive concepts, missing capability, blocked intervals, or booking conflicts.
- Responses include a stable machine-readable error code and a safe human-readable detail.

## Audit

Appointment creation, cancellation, and rescheduling append audit records containing:

- Actor identifier and actor type.
- Action.
- Entity identifier and type.
- UTC timestamp.
- Relevant before/after state.
- Correlation identifier when supplied by the request boundary.

Audit entries are not edited by normal application flows.

## Testing Strategy

Write tests before implementation for:

- Registering a lead with a valid acquisition source and service need.
- Rejecting invalid acquisition sources.
- Filtering practitioners by service capability and location.
- Excluding inactive services, practitioners, locations, and capabilities.
- Calculating slots from availability and authoritative duration.
- Excluding schedule blocks and existing confirmed appointments.
- Ignoring a client-supplied duration override.
- Confirming a valid appointment.
- Racing two confirmations for the same practitioner interval and persisting exactly one.
- Cancelling and releasing a slot.
- Rescheduling atomically and preserving an audit trail.
- Returning stable `404`, `409`, and `422` error contracts.

Use unit tests for interval and transition rules. Use PostgreSQL integration tests for overlap constraints, transactions, and concurrency. Exercise FastAPI request/response contracts through integration tests.

## Definition of Done

- PostgreSQL migrations define the minimum model and overlap guarantee.
- The FastAPI application exposes the approved behavior.
- Unit and PostgreSQL integration tests pass.
- OpenAPI describes the public surfaces and error schemas.
- Audit records are created for all required appointment transitions.
- No external adapter or LLM is on the booking decision path.
- The flow works for at least two locations and practitioners with different capabilities.
- No MediStock application code is modified.

## Deferred Questions

- Exact commercial status progression.
- Minimum booking lead time and maximum booking horizon.
- Whether practitioner capability varies by date or only by active state.
- Cancellation policy and who may cancel or reschedule.
- Authentication and role model for the first production deployment.

These questions do not justify adding speculative entities. They must be resolved before the related production behavior is considered complete.
