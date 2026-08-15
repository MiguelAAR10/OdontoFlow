"""Clinical core: patients, visits, service_executions + permission codes.

Implements the Clinical Core migration (PF5): three organization-owned tables
with the §7 composite-FK pattern — every cross-tenant relationship is
structurally impossible — plus the six new permission codes and their grants
to the seeded ``system`` role in every organization (PR7 pattern), so
platform automation stays permission-checked on the normal path.

Strictly additive: no existing table altered, ``excl_appointments_confirmed_no_overlap``
untouched. Legacy semantics preserved: DNI uniqueness adapted to the tenant
boundary (partial unique index, §7 M4 pattern); one service execution per
visit (``UNIQUE (organization_id, visit_id, service_id)``, legacy
``UNIQUE(id_consulta, id_servicio)``); executed price is a point-in-time
snapshot (NOT NULL, >= 0).
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SEXO_CHECK = "sexo IN ('M', 'F', 'O')"

# The closed clinical vocabulary (M6 convention: <domain>.<action>). Mirrors
# app/iam/permissions.py; duplicated here so the migration stays independent
# of application code, exactly like migrations 0003/0004.
CLINICAL_PERMISSION_CODES = (
    "patients.read",
    "patients.create",
    "visits.read",
    "visits.create",
    "executions.read",
    "executions.create",
)


def upgrade() -> None:
    # --- patients -----------------------------------------------------------
    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("dni", sa.String(length=20), nullable=True),
        sa.Column("sexo", sa.String(length=10), nullable=True),
        sa.Column("phone", sa.String(length=25), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
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
            name="fk_patients_organization",
        ),
        sa.CheckConstraint(SEXO_CHECK, name="ck_patients_sexo"),
        sa.UniqueConstraint("organization_id", "id", name="uq_patients_organization_id"),
    )
    # The durable clinic identity, per organization. Partial on purpose (M4):
    # NULLs are distinct in PostgreSQL, so patients without DNI are legal.
    op.create_index(
        "uq_patients_org_dni",
        "patients",
        ["organization_id", "dni"],
        unique=True,
        postgresql_where=sa.text("dni IS NOT NULL"),
    )
    op.create_index("ix_patients_org_name", "patients", ["organization_id", "full_name"])

    # --- visits -------------------------------------------------------------
    op.create_table(
        "visits",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("practitioner_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
            name="fk_visits_organization",
        ),
        # §7 composite FKs: patient, appointment, membership, location all in
        # the same organization by construction. MATCH SIMPLE (default): a
        # visit without an appointment skips that check.
        sa.ForeignKeyConstraint(
            ["organization_id", "patient_id"],
            ["patients.organization_id", "patients.id"],
            ondelete="RESTRICT",
            name="fk_visits_organization_patient",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "appointment_id"],
            ["appointments.organization_id", "appointments.id"],
            ondelete="RESTRICT",
            name="fk_visits_organization_appointment",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "practitioner_id"],
            ["practitioner_memberships.organization_id", "practitioner_memberships.practitioner_id"],
            ondelete="RESTRICT",
            name="fk_visits_organization_membership",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            ondelete="RESTRICT",
            name="fk_visits_organization_location",
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_visits_organization_id"),
    )

    # --- service_executions -------------------------------------------------
    op.create_table(
        "service_executions",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("visit_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("executed_price", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "executed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
            name="fk_service_executions_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "visit_id"],
            ["visits.organization_id", "visits.id"],
            ondelete="RESTRICT",
            name="fk_service_executions_organization_visit",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            ondelete="RESTRICT",
            name="fk_service_executions_organization_service",
        ),
        sa.CheckConstraint("executed_price >= 0", name="ck_service_executions_price"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_service_executions_organization_id",
        ),
        # Legacy UNIQUE(id_consulta, id_servicio), tenant-qualified.
        sa.UniqueConstraint(
            "organization_id",
            "visit_id",
            "service_id",
            name="uq_service_executions_org_visit_service",
        ),
    )

    # --- permission codes + system grants (M5/PR7 pattern) ------------------
    op.bulk_insert(
        sa.table(
            "permissions",
            sa.column("code", sa.String),
            sa.column("name", sa.String),
        ),
        [
            {"code": code, "name": name}
            for code, name in (
                ("patients.read", "Read patients"),
                ("patients.create", "Register patients"),
                ("visits.read", "Read visits"),
                ("visits.create", "Register visits"),
                ("executions.read", "Read service executions"),
                ("executions.create", "Record service executions"),
            )
        ],
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r "
            "CROSS JOIN permissions p "
            "WHERE r.code = :code AND p.code IN :codes"
        ).bindparams(
            sa.bindparam("code", value="system"),
            sa.bindparam("codes", value=tuple(CLINICAL_PERMISSION_CODES), expanding=True),
        )
    )


def downgrade() -> None:
    codes = sa.bindparam("codes", value=tuple(CLINICAL_PERMISSION_CODES), expanding=True)
    op.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN ("
            "  SELECT id FROM permissions WHERE code IN :codes)"
        ).bindparams(codes)
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code IN :codes").bindparams(codes)
    )
    op.drop_table("service_executions")
    op.drop_index("ix_patients_org_name", table_name="patients")
    op.drop_index("uq_patients_org_dni", table_name="patients")
    op.drop_table("visits")
    op.drop_table("patients")
