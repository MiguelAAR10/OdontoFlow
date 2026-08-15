"""Durable command idempotency via ``command_receipts``.

Implements PF4 of the platform foundation spec
(``docs/superpowers/specs/2026-08-14-platform-foundation-design.md``), §15–§16:
one table, ``command_receipts``, whose unique index is the *entire* concurrency
mechanism. Strictly additive: one new table, no existing table altered, no row
touched, ``excl_appointments_confirmed_no_overlap`` untouched.

DB invariants (§15):

* ``UNIQUE (organization_id, operation, idempotency_key)`` — the claim. The
  constraint name is a contract: ``app/idempotency/service.py`` distinguishes
  this ``23505`` from every other unique violation by
  ``diag.constraint_name`` (C7).
* composite ``(organization_id, principal_id)`` FK into
  ``memberships(organization_id, principal_id)`` — a receipt cannot exist for
  a principal that is not a member of that organization (I3); plain FKs to
  ``organizations`` and ``principals``, all RESTRICT;
* ``request_fingerprint`` NOT NULL — sha256 hex of the canonical command
  payload (I4);
* ``resource_type`` / ``resource_id`` / ``outcome_json`` NULL on claim,
  filled by the same transaction before commit (I5/I13) — a committed receipt
  always carries its logical outcome.

No PENDING state, no reaper, no advisory locks, no application-level
locking: PostgreSQL transaction visibility plus the unique index provide
exactly-once execution (§16).
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "command_receipts",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("principal_id", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=True),
        sa.Column("resource_id", sa.String(length=100), nullable=True),
        sa.Column("outcome_json", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=False),
        sa.Column("correlation_id", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_command_receipts_organization",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            ondelete="RESTRICT",
            name="fk_command_receipts_principal",
        ),
        # I3: a receipt cannot exist for a non-member principal — the
        # structural echo of authorization into the schema (§15 I3).
        sa.ForeignKeyConstraint(
            ["organization_id", "principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_command_receipts_organization_membership",
        ),
        # THE CLAIM (I1/I2). The name is load-bearing: the application
        # handler identifies receipt collisions by this exact constraint name
        # (C7), so it must never be renamed casually.
        sa.UniqueConstraint(
            "organization_id",
            "operation",
            "idempotency_key",
            name="uq_command_receipts_org_operation_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("command_receipts")
