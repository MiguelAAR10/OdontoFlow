"""The platform permission catalog (PF0 §11 M5–M8).

`permissions` rows are **code-owned**: seeded by migration ``0003`` and never
inserted at runtime. A tenant configures *roles*; the permission vocabulary is
part of the platform contract (A10).

Naming convention (M6): ``<domain>.<action>``, lowercase, dot-separated, no
wildcards and no hierarchy. ``domain`` is an application module surface (plural
noun); ``action`` comes from the reserved verb set
``read | create | update | cancel | reschedule | manage``.

No permission implies another (M8): a role that must read *and* administer
services holds both ``services.read`` and ``services.manage``, which keeps the
guard a pure set-membership test.
"""

from __future__ import annotations

APPOINTMENTS_READ = "appointments.read"
APPOINTMENTS_CREATE = "appointments.create"
APPOINTMENTS_RESCHEDULE = "appointments.reschedule"
APPOINTMENTS_CANCEL = "appointments.cancel"
PATIENTS_READ = "patients.read"
PATIENTS_CREATE = "patients.create"
VISITS_READ = "visits.read"
VISITS_CREATE = "visits.create"
EXECUTIONS_READ = "executions.read"
EXECUTIONS_CREATE = "executions.create"
PRODUCTS_READ = "products.read"
PRODUCTS_CREATE = "products.create"
CONSUMPTIONS_READ = "consumptions.read"
CONSUMPTIONS_CREATE = "consumptions.create"
CHARGES_READ = "charges.read"
CHARGES_CREATE = "charges.create"
PAYMENTS_READ = "payments.read"
PAYMENTS_CREATE = "payments.create"
SERVICES_READ = "services.read"
SERVICES_MANAGE = "services.manage"
LEADS_READ = "leads.read"
LEADS_CREATE = "leads.create"
LOCATIONS_READ = "locations.read"
LOCATIONS_MANAGE = "locations.manage"
PRACTITIONERS_READ = "practitioners.read"
PRACTITIONERS_MANAGE = "practitioners.manage"
CAPABILITIES_READ = "capabilities.read"
CAPABILITIES_MANAGE = "capabilities.manage"
AVAILABILITY_READ = "availability.read"
AVAILABILITY_MANAGE = "availability.manage"
AUDIT_READ = "audit.read"

#: The closed set seeded by PF2 (M7) — exactly the surface today's eleven
#: endpoints need, no more. Future verticals extend it under M6; PF0 does not
#: enumerate the whole ERP.
PERMISSION_CATALOG: tuple[tuple[str, str], ...] = (
    (APPOINTMENTS_READ, "Read appointments"),
    (APPOINTMENTS_CREATE, "Book appointments"),
    (APPOINTMENTS_RESCHEDULE, "Reschedule appointments"),
    (APPOINTMENTS_CANCEL, "Cancel appointments"),
    # PF5 — Clinical core (patients, visits, executed services).
    (PATIENTS_READ, "Read patients"),
    (PATIENTS_CREATE, "Register patients"),
    (VISITS_READ, "Read visits"),
    (VISITS_CREATE, "Register visits"),
    (EXECUTIONS_READ, "Read service executions"),
    (EXECUTIONS_CREATE, "Record service executions"),
    # PF6 — Economic & operations core (products, consumptions, charges, payments).
    (PRODUCTS_READ, "Read products"),
    (PRODUCTS_CREATE, "Register products"),
    (CONSUMPTIONS_READ, "Read service consumptions"),
    (CONSUMPTIONS_CREATE, "Record service consumptions"),
    (CHARGES_READ, "Read charges"),
    (CHARGES_CREATE, "Register charges"),
    (PAYMENTS_READ, "Read payments"),
    (PAYMENTS_CREATE, "Record payments"),
    (SERVICES_READ, "Read services"),
    (SERVICES_MANAGE, "Administer services"),
    (LEADS_READ, "Read leads"),
    (LEADS_CREATE, "Register leads"),
    (LOCATIONS_READ, "Read locations"),
    (LOCATIONS_MANAGE, "Administer locations"),
    (PRACTITIONERS_READ, "Read practitioners"),
    (PRACTITIONERS_MANAGE, "Administer practitioners"),
    (CAPABILITIES_READ, "Read practitioner capabilities"),
    (CAPABILITIES_MANAGE, "Administer practitioner capabilities"),
    (AVAILABILITY_READ, "Read availability"),
    (AVAILABILITY_MANAGE, "Administer availability"),
    (AUDIT_READ, "Read the audit trail"),
)

PERMISSION_CODES: tuple[str, ...] = tuple(code for code, _name in PERMISSION_CATALOG)
