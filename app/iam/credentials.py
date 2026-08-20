"""Integration credentials: the door in front of the authorization layer.

The permission model was already complete and proven — 33 codes, roles,
location scoping, composite tenant keys. What was missing was authentication:
``resolve_http_context`` returned constants, so every anonymous request became
the seeded ``system`` principal holding the whole catalog.

This module resolves a bearer token into a real ``Principal``. Three properties
matter:

* **The tenant is read from PostgreSQL, never from the request.** No header and
  no body field can influence which organization a caller acts in.
* **The secret is never stored.** Only its SHA-256 digest is. The token is
  high-entropy (256 bits from ``secrets``), so a single hash is the right
  primitive here — a slow KDF protects low-entropy human passwords, which this
  is not, and would add its cost to every single request.
* **Rejections are indistinguishable.** Unknown, revoked, expired, inactive and
  malformed all produce the same status, code and message, so the response
  cannot be used to enumerate valid prefixes.

The error is raised with an explicit ``http_status`` rather than a new entry in
``app/errors.py``: that module is the approved six-code envelope and is outside
this task's write surface. ``app/iam/service.py`` already established the
pattern with ``PERMISSION_DENIED``/403.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.errors import AppError

UTC = timezone.utc

#: Token shape: ``ofk_<prefix>_<secret>``. The prefix is stored in clear and
#: indexed, so a lookup is one indexed read instead of hashing every row.
TOKEN_NAMESPACE = "ofk"
PREFIX_LENGTH = 8
SECRET_BYTES = 32

AUTHORIZATION_HEADER = "Authorization"
BEARER_SCHEME = "bearer"


class AuthErrorCode(str, Enum):
    """Kept out of the approved envelope enum on purpose (see module docstring)."""

    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"


AUTHENTICATION_REQUIRED_HTTP_STATUS = 401
AUTHENTICATION_REQUIRED_MESSAGE = "A valid integration credential is required."


def authentication_required() -> AppError:
    """One rejection for every failure mode — no detail, ever.

    Distinguishing "unknown token" from "revoked token" would let a caller
    confirm which prefixes exist.
    """
    return AppError(
        AuthErrorCode.AUTHENTICATION_REQUIRED,  # type: ignore[arg-type]
        AUTHENTICATION_REQUIRED_MESSAGE,
        details={},
        http_status=AUTHENTICATION_REQUIRED_HTTP_STATUS,
    )


class IntegrationCredential(Base):
    """A revocable secret bound to one principal inside one organization."""

    __tablename__ = "integration_credentials"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id", ondelete="RESTRICT", name="fk_integration_credentials_organization"
        ),
        nullable=False,
    )
    principal_id: Mapped[int] = mapped_column(
        ForeignKey(
            "principals.id", ondelete="RESTRICT", name="fk_integration_credentials_principal"
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Clear-text lookup handle. Not a secret: it only narrows the search.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    #: SHA-256 hex of the secret half. The secret itself is shown once, at issue.
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("prefix", name="uq_integration_credentials_prefix"),
        CheckConstraint("length(secret_hash) = 64", name="ck_integration_credentials_hash_len"),
        # The credential may only name a principal that already belongs to the
        # organization: authority still comes from the membership, never from
        # the credential row (PF0 §7.1).
        ForeignKeyConstraint(
            ["organization_id", "principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_integration_credentials_membership",
        ),
    )


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def build_token(prefix: str, secret: str) -> str:
    return f"{TOKEN_NAMESPACE}_{prefix}_{secret}"


def split_token(token: str) -> tuple[str, str] | None:
    """Return ``(prefix, secret)`` or ``None`` when the shape is not ours."""
    # maxsplit=2: ``token_urlsafe`` draws from the base64url alphabet, which
    # includes "_", so the secret itself may contain separators. Splitting
    # greedily made authentication fail at random.
    parts = token.split("_", 2)
    if len(parts) != 3:
        return None
    namespace, prefix, secret = parts
    if namespace != TOKEN_NAMESPACE or not prefix or not secret:
        return None
    return prefix, secret


def bearer_token(header_value: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header, or ``None``."""
    if not header_value:
        return None
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != BEARER_SCHEME:
        return None
    value = value.strip()
    return value or None


def issue_credential(
    session: Session,
    *,
    organization_id: int,
    principal_id: int,
    name: str,
    expires_at: datetime | None = None,
) -> tuple[IntegrationCredential, str]:
    """Create a credential and return it with its plaintext token.

    The plaintext is returned exactly once and never persisted; callers are
    responsible for handing it to the integration and forgetting it.
    """
    prefix = secrets.token_hex(PREFIX_LENGTH // 2)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    credential = IntegrationCredential(
        organization_id=organization_id,
        principal_id=principal_id,
        name=name,
        prefix=prefix,
        secret_hash=hash_secret(secret),
        expires_at=expires_at,
    )
    session.add(credential)
    session.flush()
    return credential, build_token(prefix, secret)


def revoke_credential(session: Session, credential_id: int) -> None:
    credential = session.get(IntegrationCredential, credential_id)
    if credential is not None:
        credential.revoked_at = datetime.now(UTC)
        session.flush()


def authenticate(session: Session, token: str | None) -> IntegrationCredential:
    """Resolve a bearer token into a usable credential, or raise 401.

    Every rejection path raises the identical error (see
    :func:`authentication_required`).
    """
    if not token:
        raise authentication_required()
    parsed = split_token(token)
    if parsed is None:
        raise authentication_required()
    prefix, secret = parsed

    credential = session.scalar(
        select(IntegrationCredential).where(IntegrationCredential.prefix == prefix)
    )
    if credential is None:
        # Still spend a hash so a missing prefix and a wrong secret take a
        # comparable amount of time.
        hash_secret(secret)
        raise authentication_required()

    if not secrets.compare_digest(credential.secret_hash, hash_secret(secret)):
        raise authentication_required()
    if not credential.is_active or credential.revoked_at is not None:
        raise authentication_required()
    if credential.expires_at is not None and credential.expires_at <= datetime.now(UTC):
        raise authentication_required()

    principal = credential.principal_id and session.get(_principal_model(), credential.principal_id)
    if principal is None or not principal.is_active:
        raise authentication_required()

    return credential


def _principal_model():
    """Imported lazily so this module stays importable from ``app.iam.models``."""
    from app.iam.models import Principal

    return Principal
