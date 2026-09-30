"""B2 generic agent proposals (human approval inbox).

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

KINDS = "kind IN ('collection_reminder', 'collection_follow_up')"
STATUSES = (
    "status IN ('pending', 'approved', 'executed', 'failed', 'declined', 'expired', "
    "'superseded')"
)
OPEN_STATUSES = "status IN ('pending', 'approved')"

PERMISSIONS = (
    ("proposals.read", "Read agent proposals and the approval inbox"),
    ("proposals.create", "Create agent proposals for human approval"),
    ("proposals.decide", "Approve or decline agent proposals"),
)
CODES = [code for code, _name in PERMISSIONS]


def _tenant_fk(column: str, target: str, suffix: str, target_column: str = "id"):
    return sa.ForeignKeyConstraint(
        ["organization_id", column],
        [f"{target}.organization_id", f"{target}.{target_column}"],
        ondelete="RESTRICT",
        name=f"fk_agent_proposals_organization_{suffix}",
    )


def upgrade() -> None:
    op.create_table(
        "agent_proposals",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("agent_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("subject_version", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("execution_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposed_by_principal_id", sa.Integer(), nullable=False),
        sa.Column("decided_by_principal_id", sa.Integer(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("result_ref", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(KINDS, name="ck_agent_proposals_kind"),
        sa.CheckConstraint(STATUSES, name="ck_agent_proposals_status"),
        sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="ck_agent_proposals_payload_hash"),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 500", name="ck_agent_proposals_reason"
        ),
        sa.CheckConstraint("length(btrim(agent_key)) > 0", name="ck_agent_proposals_agent_key"),
        sa.CheckConstraint(
            "decision_note IS NULL OR length(decision_note) <= 500",
            name="ck_agent_proposals_decision_note",
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'executed', 'failed', 'declined') "
            "OR decided_by_principal_id IS NOT NULL",
            name="ck_agent_proposals_decided",
        ),
        sa.CheckConstraint(
            "status <> 'executed' OR result_ref IS NOT NULL", name="ck_agent_proposals_result"
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL", name="ck_agent_proposals_error"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_agent_proposals_expiry"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_agent_proposals_organization",
        ),
        _tenant_fk("location_id", "locations", "location"),
        _tenant_fk("conversation_id", "conversations", "conversation"),
        _tenant_fk(
            "proposed_by_principal_id", "memberships", "proposer", target_column="principal_id"
        ),
        _tenant_fk(
            "decided_by_principal_id", "memberships", "decider", target_column="principal_id"
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_agent_proposals_organization_id"),
        sa.UniqueConstraint("execution_key", name="uq_agent_proposals_execution_key"),
    )
    op.create_index(
        "uq_agent_proposals_open_dedupe",
        "agent_proposals",
        ["organization_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text(OPEN_STATUSES),
    )
    op.create_index(
        "ix_agent_proposals_inbox",
        "agent_proposals",
        ["organization_id", "status", sa.text("created_at DESC"), sa.text("id DESC")],
    )

    permission_table = sa.table(
        "permissions", sa.column("code", sa.String()), sa.column("name", sa.String())
    )
    op.bulk_insert(permission_table, [{"code": code, "name": name} for code, name in PERMISSIONS])
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.code = 'system' AND p.code = ANY(:codes)"
        ).bindparams(codes=CODES)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM role_permissions rp USING permissions p "
            "WHERE rp.permission_id = p.id AND p.code = ANY(:codes)"
        ).bindparams(codes=CODES)
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)").bindparams(codes=CODES)
    )
    op.drop_index("ix_agent_proposals_inbox", table_name="agent_proposals")
    op.drop_index("uq_agent_proposals_open_dedupe", table_name="agent_proposals")
    op.drop_table("agent_proposals")
