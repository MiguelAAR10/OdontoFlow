"""The single PF1 tenant-resolution seam.

PF1 makes ``Organization`` the tenant root while **no identity mechanism
exists yet** (Principal is PF2, ``ExecutionContext`` is PF3). Every write site
and every tenant-scoped read therefore needs an ``organization_id`` that no
HTTP caller may supply: PF0 §21 excludes request/response schema changes from
this block, so the transport cannot carry a tenant field and the seam below is
the *only* place the bootstrap default is expressed.

PF3 replaces the body of :func:`resolve_organization_id` with
``ctx.organization_id`` and deletes the ``organization_id=None`` defaults from
the application services; nothing else about tenancy moves.
"""

from __future__ import annotations

#: The organization seeded by migration ``0002`` — the single implicit tenant
#: that every Vertical 1 row was backfilled into. Deterministic by construction:
#: it is the first row of ``organizations``.
BOOTSTRAP_ORGANIZATION_ID = 1
BOOTSTRAP_ORGANIZATION_NAME = "Bootstrap Clinic"


def resolve_organization_id(organization_id: int | None = None) -> int:
    """Return the organization a command or query acts within.

    An explicit ``organization_id`` always wins (services pass it down, tests
    and future transports supply it); ``None`` falls back to the bootstrap
    organization, which is exactly the pre-PF1 behaviour of a single implicit
    tenant.
    """
    if organization_id is not None:
        return organization_id
    return BOOTSTRAP_ORGANIZATION_ID


def scoped(statement, model, organization_id: int):
    """Add the mandatory tenant filter to a select over a tenant-owned model.

    Composite foreign keys make a cross-tenant *write* impossible (PF0 §7), but
    they do not scope *reads* (§7.4). This is the one helper every tenant-scoped
    read goes through, so the filter is greppable and cannot be forgotten
    silently.
    """
    return statement.where(model.organization_id == organization_id)
