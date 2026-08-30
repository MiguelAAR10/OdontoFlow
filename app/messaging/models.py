"""Phase 2 messaging persistence with tenant integrity enforced by PostgreSQL."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChannelAccount(Base):
    __tablename__ = "channel_accounts"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_channel_accounts_organization",
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    phone_number_id: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("provider IN ('whatsapp')", name="ck_channel_accounts_provider"),
        UniqueConstraint(
            "organization_id", "id", name="uq_channel_accounts_organization_id"
        ),
        UniqueConstraint(
            "organization_id",
            "provider",
            "external_account_id",
            name="uq_channel_accounts_provider_external",
        ),
        Index(
            "uq_channel_accounts_phone_number",
            "organization_id",
            "provider",
            "phone_number_id",
            unique=True,
            postgresql_where=text("phone_number_id IS NOT NULL"),
        ),
    )


class ContactIdentity(Base):
    __tablename__ = "contact_identities"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_contact_identities_organization",
        ),
        nullable=False,
    )
    channel_account_id: Mapped[int] = mapped_column(nullable=False)
    external_contact_id: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    lead_id: Mapped[int | None] = mapped_column()
    patient_id: Mapped[int | None] = mapped_column()
    consent_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "consent_status IN ('unknown', 'pending', 'opted_in', 'opted_out')",
            name="ck_contact_identities_consent_status",
        ),
        CheckConstraint(
            "normalized_phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'",
            name="ck_contact_identities_phone_e164",
        ),
        UniqueConstraint(
            "organization_id", "id", name="uq_contact_identities_organization_id"
        ),
        UniqueConstraint(
            "organization_id",
            "channel_account_id",
            "external_contact_id",
            name="uq_contact_identities_channel_external",
        ),
        ForeignKeyConstraint(
            ["organization_id", "channel_account_id"],
            ["channel_accounts.organization_id", "channel_accounts.id"],
            ondelete="RESTRICT",
            name="fk_contact_identities_organization_channel",
        ),
        ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            ondelete="RESTRICT",
            name="fk_contact_identities_organization_lead",
        ),
        ForeignKeyConstraint(
            ["organization_id", "patient_id"],
            ["patients.organization_id", "patients.id"],
            ondelete="RESTRICT",
            name="fk_contact_identities_organization_patient",
        ),
        Index(
            "ix_contact_identities_org_phone",
            "organization_id",
            "normalized_phone_e164",
        ),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_conversations_organization",
        ),
        nullable=False,
    )
    channel_account_id: Mapped[int] = mapped_column(nullable=False)
    contact_identity_id: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="open")
    assigned_principal_id: Mapped[int | None] = mapped_column()
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'awaiting_confirmation', 'human_handoff', 'closed')",
            name="ck_conversations_status",
        ),
        UniqueConstraint(
            "organization_id", "id", name="uq_conversations_organization_id"
        ),
        UniqueConstraint(
            "organization_id",
            "channel_account_id",
            "id",
            name="uq_conversations_organization_channel_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "channel_account_id"],
            ["channel_accounts.organization_id", "channel_accounts.id"],
            ondelete="RESTRICT",
            name="fk_conversations_organization_channel",
        ),
        ForeignKeyConstraint(
            ["organization_id", "contact_identity_id"],
            ["contact_identities.organization_id", "contact_identities.id"],
            ondelete="RESTRICT",
            name="fk_conversations_organization_contact",
        ),
        ForeignKeyConstraint(
            ["organization_id", "assigned_principal_id"],
            ["memberships.organization_id", "memberships.principal_id"],
            ondelete="RESTRICT",
            name="fk_conversations_organization_assignee",
        ),
        Index(
            "uq_conversations_active_contact",
            "organization_id",
            "channel_account_id",
            "contact_identity_id",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
        ),
        Index("ix_conversations_org_last_message", "organization_id", "last_message_at"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_messages_organization",
        ),
        nullable=False,
    )
    channel_account_id: Mapped[int] = mapped_column(nullable=False)
    conversation_id: Mapped[int] = mapped_column(nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    message_type: Mapped[str] = mapped_column(String(20), nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    media_reference: Mapped[dict | None] = mapped_column(JSONB)
    delivery_status: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_redacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="ck_messages_direction"
        ),
        CheckConstraint(
            "message_type IN ('text', 'audio', 'image', 'template', 'system')",
            name="ck_messages_type",
        ),
        CheckConstraint(
            "delivery_status IN ('received', 'pending', 'processing', 'sent', "
            "'delivered', 'read', 'failed', 'dead_letter')",
            name="ck_messages_delivery_status",
        ),
        CheckConstraint(
            "direction <> 'inbound' OR provider_message_id IS NOT NULL",
            name="ck_messages_inbound_provider_id",
        ),
        UniqueConstraint("organization_id", "id", name="uq_messages_organization_id"),
        UniqueConstraint(
            "organization_id",
            "conversation_id",
            "id",
            name="uq_messages_organization_conversation_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "channel_account_id", "conversation_id"],
            [
                "conversations.organization_id",
                "conversations.channel_account_id",
                "conversations.id",
            ],
            ondelete="RESTRICT",
            name="fk_messages_organization_channel_conversation",
        ),
        Index(
            "uq_messages_provider_id",
            "organization_id",
            "channel_account_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index("ix_messages_conversation_occurred", "conversation_id", "occurred_at"),
        Index("ix_messages_content_expiry", "content_expires_at"),
    )


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="RESTRICT",
            name="fk_outbound_messages_organization",
        ),
        nullable=False,
    )
    conversation_id: Mapped[int] = mapped_column(nullable=False)
    message_id: Mapped[int] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_result_idempotency_key: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'delivered', 'failed', "
            "'dead_letter')",
            name="ck_outbound_messages_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_outbound_messages_attempt_count"),
        CheckConstraint(
            "idempotency_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            "[89ab][0-9a-f]{3}-[0-9a-f]{12}$'",
            name="ck_outbound_messages_idempotency_uuid4",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_outbound_messages_organization_idempotency",
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id", "message_id"],
            ["messages.organization_id", "messages.conversation_id", "messages.id"],
            ondelete="RESTRICT",
            name="fk_outbound_messages_organization_message",
        ),
        Index(
            "ix_outbound_messages_dispatch",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
    )


class ReceptionHandoff(Base):
    """One actionable request for a human receptionist to take over."""

    __tablename__ = "reception_handoffs"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(nullable=False)
    contact_identity_id: Mapped[int] = mapped_column(nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "reason_code IN ('requested_by_contact', 'urgent_symptoms', 'complaint', "
            "'pricing_exception', 'clinical_case', 'low_confidence', 'other')",
            name="ck_reception_handoffs_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'resolved')",
            name="ck_reception_handoffs_status",
        ),
        UniqueConstraint(
            "organization_id", "id", name="uq_reception_handoffs_organization_id"
        ),
        ForeignKeyConstraint(
            ["organization_id", "conversation_id"],
            ["conversations.organization_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_reception_handoffs_organization_conversation",
        ),
        ForeignKeyConstraint(
            ["organization_id", "contact_identity_id"],
            ["contact_identities.organization_id", "contact_identities.id"],
            ondelete="RESTRICT",
            name="fk_reception_handoffs_organization_contact",
        ),
        Index(
            "uq_reception_handoffs_pending_conversation",
            "organization_id",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'claimed')"),
        ),
    )

