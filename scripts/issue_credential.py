"""Issue, list and revoke integration credentials.

Authentication is worthless without a way to hand out and take back keys, so
this is part of the same change rather than a follow-up.

    python scripts/issue_credential.py issue --name n8n-inbound --profile n8n-inbound
    python scripts/issue_credential.py list
    python scripts/issue_credential.py revoke --id 3

The plaintext token is printed **once** and never stored — only its SHA-256
digest reaches PostgreSQL. If it is lost, revoke the credential and issue a new
one; there is deliberately no way to recover it.

A credential can only name a principal that already belongs to the
organization, because the row carries a composite foreign key into
``memberships``. Each issued credential must select one least-privilege
profile; the script reconciles that profile's role to its exact permission set.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.iam.credentials import (  # noqa: E402
    IntegrationCredential,
    issue_credential,
    revoke_credential,
)
from app.iam.models import (  # noqa: E402
    Membership,
    Permission,
    Principal,
    PRINCIPAL_TYPES,
    Role,
    RoleAssignment,
    RolePermission,
)
from app.iam.permissions import (  # noqa: E402
    AVAILABILITY_READ,
    CONTACT_APPOINTMENTS_BOOK,
    CONTACT_APPOINTMENTS_CANCEL,
    CONTACT_APPOINTMENTS_READ,
    CONTACT_APPOINTMENTS_RESCHEDULE,
    CONTACT_PROFILES_MANAGE,
    CONVERSATIONS_MANAGE,
    CONVERSATIONS_READ,
    DELIVERIES_CREATE,
    DELIVERIES_MANAGE,
    MESSAGES_CREATE,
    LOCATIONS_READ,
    PRACTITIONERS_READ,
    SERVICES_READ,
)
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID  # noqa: E402

UTC = timezone.utc
ISSUABLE_TYPES = ("integration", "agent")
PROFILE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "connectivity": (SERVICES_READ,),
    "n8n-inbound": (MESSAGES_CREATE,),
    "conversation-agent": (
        CONVERSATIONS_READ,
        DELIVERIES_CREATE,
        SERVICES_READ,
        LOCATIONS_READ,
        PRACTITIONERS_READ,
        AVAILABILITY_READ,
        CONTACT_APPOINTMENTS_READ,
        CONTACT_APPOINTMENTS_BOOK,
        CONTACT_APPOINTMENTS_CANCEL,
        CONTACT_APPOINTMENTS_RESCHEDULE,
        CONTACT_PROFILES_MANAGE,
        CONVERSATIONS_MANAGE,
    ),
    "outbound-dispatcher": (DELIVERIES_MANAGE,),
}


def _resolve_principal(
    session: Session, *, organization_id: int, name: str, principal_type: str
) -> Principal:
    """Find the named principal in this organization, or create it as a member.

    Creating the membership here is deliberate: a credential whose principal is
    not a member could never authorize anything, so issuing one would only
    produce a confusing 403 later.
    """
    principal = session.scalar(
        select(Principal)
        .join(Membership, Membership.principal_id == Principal.id)
        .where(
            Membership.organization_id == organization_id,
            Principal.display_name == name,
            Principal.type == principal_type,
        )
    )
    if principal is not None:
        return principal

    principal = session.scalar(
        select(Principal).where(
            Principal.display_name == name, Principal.type == principal_type
        )
    )
    if principal is None:
        principal = Principal(type=principal_type, display_name=name)
        session.add(principal)
        session.flush()

    membership = session.scalar(
        select(Membership).where(
            Membership.organization_id == organization_id,
            Membership.principal_id == principal.id,
        )
    )
    if membership is None:
        session.add(
            Membership(organization_id=organization_id, principal_id=principal.id)
        )
        session.flush()
    return principal


def _assign_profile(
    session: Session,
    *,
    organization_id: int,
    principal_id: int,
    profile: str,
) -> None:
    """Assign one organization-wide role whose permissions exactly match a profile."""
    permission_codes = PROFILE_PERMISSIONS[profile]
    permissions = session.scalars(
        select(Permission).where(Permission.code.in_(permission_codes))
    ).all()
    found_codes = {permission.code for permission in permissions}
    missing = set(permission_codes) - found_codes
    if missing:
        raise RuntimeError(
            "Run the latest Alembic migrations before issuing this profile; "
            f"missing permissions: {', '.join(sorted(missing))}."
        )

    role_code = f"integration-{profile}"
    role = session.scalar(
        select(Role).where(
            Role.organization_id == organization_id,
            Role.code == role_code,
        )
    )
    if role is None:
        role = Role(
            organization_id=organization_id,
            code=role_code,
            name=f"Integration: {profile}",
        )
        session.add(role)
        session.flush()

    session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    session.add_all(
        RolePermission(role_id=role.id, permission_id=permission.id)
        for permission in permissions
    )

    membership = session.scalar(
        select(Membership).where(
            Membership.organization_id == organization_id,
            Membership.principal_id == principal_id,
        )
    )
    assert membership is not None
    other_assignment = session.scalar(
        select(RoleAssignment)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(
            RoleAssignment.organization_id == organization_id,
            RoleAssignment.membership_id == membership.id,
            Role.code != role_code,
        )
        .limit(1)
    )
    if other_assignment is not None:
        raise RuntimeError(
            "This principal already has another role. Use one principal and "
            "credential per integration responsibility."
        )
    assignment = session.scalar(
        select(RoleAssignment).where(
            RoleAssignment.organization_id == organization_id,
            RoleAssignment.membership_id == membership.id,
            RoleAssignment.role_id == role.id,
            RoleAssignment.location_id.is_(None),
        )
    )
    if assignment is None:
        session.add(
            RoleAssignment(
                organization_id=organization_id,
                membership_id=membership.id,
                role_id=role.id,
                location_id=None,
            )
        )
    session.flush()


def cmd_issue(args: argparse.Namespace) -> int:
    if args.type not in ISSUABLE_TYPES:
        print(f"--type must be one of {ISSUABLE_TYPES}; 'system' is never issuable.")
        return 2
    if args.type not in PRINCIPAL_TYPES:
        print(f"unknown principal type: {args.type}")
        return 2

    expires_at = (
        datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None
    )
    with SessionLocal() as session:
        principal = _resolve_principal(
            session,
            organization_id=args.organization,
            name=args.name,
            principal_type=args.type,
        )
        _assign_profile(
            session,
            organization_id=args.organization,
            principal_id=principal.id,
            profile=args.profile,
        )
        credential, token = issue_credential(
            session,
            organization_id=args.organization,
            principal_id=principal.id,
            name=args.name,
            expires_at=expires_at,
        )
        session.commit()

        print("Credencial creada.")
        print(f"  id            {credential.id}")
        print(f"  organización  {credential.organization_id}")
        print(f"  principal     {principal.id} ({principal.type}) {principal.display_name}")
        print(f"  perfil        {args.profile}")
        print(f"  expira        {expires_at.isoformat() if expires_at else 'nunca'}")
        print()
        print("  TOKEN (se muestra una sola vez):")
        print(f"  {token}")
        print()
        print("  Uso:  Authorization: Bearer <token>")
        print("  El perfil ya quedó asignado con permisos mínimos.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        rows = session.scalars(
            select(IntegrationCredential)
            .where(IntegrationCredential.organization_id == args.organization)
            .order_by(IntegrationCredential.id)
        ).all()
        if not rows:
            print("Sin credenciales en esta organización.")
            return 0
        print(f"{'id':>4}  {'nombre':<22} {'prefijo':<10} {'estado':<10} último uso")
        for row in rows:
            if row.revoked_at is not None:
                state = "revocada"
            elif not row.is_active:
                state = "inactiva"
            elif row.expires_at is not None and row.expires_at <= datetime.now(UTC):
                state = "expirada"
            else:
                state = "activa"
            used = row.last_used_at.isoformat(timespec="minutes") if row.last_used_at else "nunca"
            print(f"{row.id:>4}  {row.name:<22} {row.prefix:<10} {state:<10} {used}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        credential = session.get(IntegrationCredential, args.id)
        if credential is None:
            print(f"No existe la credencial {args.id}.")
            return 1
        revoke_credential(session, args.id)
        session.commit()
        print(f"Credencial {args.id} ({credential.name}) revocada. El token deja de servir de inmediato.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--organization", type=int, default=BOOTSTRAP_ORGANIZATION_ID,
        help="Organización sobre la que actúa la credencial.",
    )

    issue = sub.add_parser("issue", parents=[common], help="Emitir una credencial nueva.")
    issue.add_argument("--name", required=True, help="Nombre del principal, p. ej. n8n-inbound.")
    issue.add_argument("--type", default="integration", help=f"Uno de {ISSUABLE_TYPES}.")
    issue.add_argument(
        "--profile",
        required=True,
        choices=tuple(PROFILE_PERMISSIONS),
        help="Perfil de permisos mínimos para esta responsabilidad.",
    )
    issue.add_argument("--expires-days", type=int, default=None, help="Caducidad en días.")
    issue.set_defaults(func=cmd_issue)

    listing = sub.add_parser("list", parents=[common], help="Listar credenciales.")
    listing.set_defaults(func=cmd_list)

    revoke = sub.add_parser("revoke", parents=[common], help="Revocar una credencial.")
    revoke.add_argument("--id", type=int, required=True)
    revoke.set_defaults(func=cmd_revoke)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
