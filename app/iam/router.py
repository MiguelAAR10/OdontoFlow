"""``GET /me`` — the authenticated principal reads its own identity (IDN).

The UI decides which modules to show from ``permissions`` alone; ``roles`` are
labels for display, never logic (M9). Read-only: no transaction, no audit row,
no permission check (a principal may always read itself).

The handler takes :func:`require_authenticated_context` directly (FastAPI caches
it per request with the router-level dependency), never ``resolve_http_context``:
a mis-mount must fail closed with 401, not fall back to the ``system`` principal
under ``ERP_ANONYMOUS_COMPAT``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.context import require_authenticated_context
from app.db import get_db
from app.iam.context import ExecutionContext
from app.iam.models import Membership, Principal, Role, RoleAssignment
from app.iam.service import effective_permission_codes
from app.organization.models import Organization

router = APIRouter(tags=["identity"])


class MePrincipal(BaseModel):
    id: int
    type: str
    display_name: str


class MeOrganization(BaseModel):
    id: int
    name: str


class MeRole(BaseModel):
    code: str
    name: str


class MeRead(BaseModel):
    principal: MePrincipal
    organization: MeOrganization
    roles: list[MeRole]
    permissions: list[str]


@router.get("/me", response_model=MeRead)
def read_me(
    ctx: ExecutionContext = Depends(require_authenticated_context),
    db: Session = Depends(get_db),
) -> MeRead:
    principal = db.get(Principal, ctx.principal_id)
    organization = db.get(Organization, ctx.organization_id)
    # Only the membership of *this* credential's organization, and only while
    # it is active — a principal with memberships elsewhere sees none of them.
    roles = db.execute(
        select(Role.code, Role.name)
        .join(RoleAssignment, RoleAssignment.role_id == Role.id)
        .join(Membership, Membership.id == RoleAssignment.membership_id)
        .where(
            Membership.organization_id == ctx.organization_id,
            Membership.principal_id == ctx.principal_id,
            Membership.is_active.is_(True),
        )
        .distinct()
        .order_by(Role.code)
    ).all()
    permissions = sorted(
        effective_permission_codes(db, ctx.principal_id, ctx.organization_id)
    )
    return MeRead(
        principal=MePrincipal(
            id=principal.id, type=principal.type, display_name=principal.display_name
        ),
        organization=MeOrganization(id=organization.id, name=organization.name),
        roles=[MeRole(code=code, name=name) for code, name in roles],
        permissions=permissions,
    )
