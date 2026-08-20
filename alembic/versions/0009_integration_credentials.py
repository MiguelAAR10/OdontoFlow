"""Integration credentials — the authentication binding the transport lacked.

Until now ``resolve_http_context`` returned constants, so every anonymous HTTP
request resolved to the seeded ``system`` principal, which migration ``0003``
granted the entire permission catalog. The authorization layer was complete and
proven; there was simply nothing in front of it.

This migration is purely **additive**: one new table, no column dropped, no
existing constraint touched. Nothing in the domain schema changes.

Two design points worth keeping:

1. **The credential cannot create authority.** Its composite foreign key lands
   on ``memberships(organization_id, principal_id)``, so a credential can only
   name a principal that already belongs to that organization. Authority keeps
   coming from membership plus role assignment — never from this row.
2. **Only the digest is stored.** ``secret_hash`` is the SHA-256 hex of a
   256-bit random secret; ``prefix`` is a non-secret lookup handle so
   authentication is one indexed read rather than a scan.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_credentials",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey(
                "organizations.id",
                ondelete="RESTRICT",
                name="fk_integration_credentials_organization",
            ),
            nullable=False,
        ),
        sa.Column(
            "principal_id",
            sa.Integer(),
            sa.ForeignKey(
                "principals.id",
                ondelete="RESTRICT",
                name="fk_integration_credentials_principal",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("prefix", name="uq_integration_credentials_prefix"),
        sa.CheckConstraint("length(secret_hash) = 64", name="ck_integration_credentials_hash_len"),
        # A credential may only name a principal already a member of the
        # organization it acts in: the credential proves *who*, membership and
        # role assignment still decide *what*.
        sa.ForeignKeyConstraint(
            ["organization_id", "principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_integration_credentials_membership",
        ),
    )
    op.create_index(
        "ix_integration_credentials_organization",
        "integration_credentials",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_credentials_organization", table_name="integration_credentials"
    )
    op.drop_table("integration_credentials")
