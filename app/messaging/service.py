"""Deterministic Phase 2 messaging services and PostgreSQL outbound queue."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import (
    DateTime,
    Integer,
    and_,
    exists,
    func,
    literal,
    or_,
    select,
    text,
    true,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from app.audit.service import record_event
from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.models import Principal
from app.iam.permissions import (
    CONVERSATIONS_MANAGE,
    CONVERSATIONS_READ,
    CONVERSATIONS_RESUME,
    DELIVERIES_CREATE,
    DELIVERIES_MANAGE,
    MESSAGES_CREATE,
)
from app.iam.service import require_permission
from app.idempotency.service import IdempotencyClaim, claim_receipt, settle_receipt
from app.messaging.models import (
    ChannelAccount,
    ContactIdentity,
    Conversation,
    Message,
    OutboundMessage,
    ReceptionHandoff,
    SandboxDeliveryReceipt,
)
from app.messaging.schemas import (
    ConversationStatus,
    InboundMessageCreate,
    InboundReceipt,
    OutboundDispatchItem,
    OutboundReceipt,
    OutboundResultCreate,
    OutboundStatusRead,
    SandboxDeliveryReceiptRead,
    SandboxOutboundPayload,
)

UTC = timezone.utc
MAX_OUTBOUND_ATTEMPTS = 3
PROCESSING_LEASE = timedelta(minutes=5)
CONVERSATION_STATUSES = frozenset(
    {"open", "awaiting_confirmation", "human_handoff", "closed"}
)
DISPATCHABLE_PROVIDERS = frozenset({"whatsapp", "sandbox"})
MAX_CONVERSATION_LIST_LIMIT = 100


def _now() -> datetime:
    return datetime.now(UTC)


def list_conversations(
    session: Session,
    *,
    ctx: ExecutionContext,
    status: ConversationStatus | None = None,
    last_message_before: datetime | None = None,
    limit: int = 50,
) -> list[Conversation]:
    """List one tenant's conversations for deterministic follow-up selection."""
    if limit < 1 or limit > MAX_CONVERSATION_LIST_LIMIT:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f"Conversation limit must be between 1 and {MAX_CONVERSATION_LIST_LIMIT}.",
        )
    if status is not None and status not in CONVERSATION_STATUSES:
        raise AppError(ErrorCode.INVALID_INPUT, "Conversation status is invalid.")
    if last_message_before is not None:
        if (
            last_message_before.tzinfo is None
            or last_message_before.utcoffset() is None
        ):
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "last_message_before must be timezone-aware.",
            )
        last_message_before = last_message_before.astimezone(UTC)

    statement = select(Conversation).where(
        Conversation.organization_id == ctx.organization_id
    )
    if status is not None:
        statement = statement.where(Conversation.status == status)
    if last_message_before is not None:
        statement = statement.where(Conversation.last_message_at < last_message_before)
    statement = statement.order_by(
        Conversation.last_message_at.asc(), Conversation.id.asc()
    ).limit(limit)

    with session.begin():
        require_permission(session, ctx, CONVERSATIONS_READ)
        return list(session.scalars(statement))


def close_conversation(
    session: Session,
    *,
    conversation_id: int,
    ctx: ExecutionContext,
    idempotency: IdempotencyClaim | None = None,
) -> dict:
    """Close one tenant-scoped conversation atomically and audibly."""
    now = _now()
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_permission(session, ctx, CONVERSATIONS_MANAGE)
        conversation = session.execute(
            select(Conversation)
            .where(
                Conversation.organization_id == ctx.organization_id,
                Conversation.id == conversation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if conversation is None:
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found.")
        if conversation.status == "closed":
            raise AppError(ErrorCode.ENTITY_INACTIVE, "Conversation is already closed.")

        before_state = {"status": conversation.status}
        conversation.status = "closed"
        conversation.updated_at = now
        session.flush()
        record_event(
            session,
            ctx=ctx,
            entity_type="conversation",
            entity_id=str(conversation.id),
            action="conversation.closed",
            before_state=before_state,
            after_state={"status": conversation.status},
        )
        outcome = {
            "conversation_id": conversation.id,
            "status": conversation.status,
        }
        settle_receipt(
            receipt,
            resource_type="conversation",
            resource_id=str(conversation.id),
            outcome_json=outcome,
        )
    return outcome


def _load_channel(session: Session, data: InboundMessageCreate, organization_id: int):
    channel = session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.organization_id == organization_id,
            ChannelAccount.provider == data.provider,
            ChannelAccount.external_account_id == data.channel_account_external_id,
        )
    )
    if channel is None:
        raise AppError(ErrorCode.NOT_FOUND, "Channel account not found.")
    if not channel.is_active:
        raise AppError(ErrorCode.ENTITY_INACTIVE, "Channel account is inactive.")
    return channel


def _existing_inbound(
    session: Session, organization_id: int, channel_id: int, provider_message_id: str
):
    message = session.scalar(
        select(Message).where(
            Message.organization_id == organization_id,
            Message.channel_account_id == channel_id,
            Message.provider_message_id == provider_message_id,
        )
    )
    if message is None:
        return None
    contact_id = session.scalar(
        select(Conversation.contact_identity_id).where(
            Conversation.organization_id == organization_id,
            Conversation.id == message.conversation_id,
        )
    )
    return InboundReceipt(
        message_id=message.id,
        conversation_id=message.conversation_id,
        contact_identity_id=contact_id,
        duplicate=True,
    )


def ingest_inbound_message(
    session: Session,
    data: InboundMessageCreate,
    *,
    ctx: ExecutionContext,
) -> InboundReceipt:
    """Persist an inbound provider event exactly once and resolve durable identity."""
    retention = timedelta(days=get_settings().message_content_retention_days)
    with session.begin():
        require_permission(session, ctx, MESSAGES_CREATE)
        channel = _load_channel(session, data, ctx.organization_id)

        existing = _existing_inbound(
            session,
            ctx.organization_id,
            channel.id,
            data.provider_message_id,
        )
        if existing is not None:
            return existing

        contact_id = session.scalar(
            pg_insert(ContactIdentity)
            .values(
                organization_id=ctx.organization_id,
                channel_account_id=channel.id,
                external_contact_id=data.external_contact_id,
                normalized_phone_e164=data.phone_e164,
                consent_status="unknown",
            )
            .on_conflict_do_update(
                constraint="uq_contact_identities_channel_external",
                set_={
                    "normalized_phone_e164": data.phone_e164,
                    "updated_at": func.now(),
                },
            )
            .returning(ContactIdentity.id)
        )

        conversation_id = session.scalar(
            pg_insert(Conversation)
            .values(
                organization_id=ctx.organization_id,
                channel_account_id=channel.id,
                contact_identity_id=contact_id,
                status="open",
                last_message_at=data.occurred_at,
            )
            .on_conflict_do_update(
                index_elements=[
                    Conversation.organization_id,
                    Conversation.channel_account_id,
                    Conversation.contact_identity_id,
                ],
                index_where=text("status <> 'closed'"),
                set_={
                    "last_message_at": func.greatest(
                        Conversation.last_message_at, data.occurred_at
                    ),
                    "updated_at": func.now(),
                },
            )
            .returning(Conversation.id)
        )

        message_id = session.scalar(
            pg_insert(Message)
            .values(
                organization_id=ctx.organization_id,
                channel_account_id=channel.id,
                conversation_id=conversation_id,
                direction="inbound",
                provider_message_id=data.provider_message_id,
                message_type=data.message_type,
                body_text=data.text,
                media_reference=(
                    data.media.model_dump(mode="json") if data.media is not None else None
                ),
                delivery_status="received",
                occurred_at=data.occurred_at,
                content_expires_at=data.occurred_at + retention,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    Message.organization_id,
                    Message.channel_account_id,
                    Message.provider_message_id,
                ],
                index_where=text("provider_message_id IS NOT NULL"),
            )
            .returning(Message.id)
        )
        if message_id is None:
            duplicate = _existing_inbound(
                session,
                ctx.organization_id,
                channel.id,
                data.provider_message_id,
            )
            if duplicate is None:  # defensive: the unique conflict guarantees it
                raise RuntimeError("Inbound deduplication row disappeared.")
            return duplicate

        record_event(
            session,
            ctx=ctx,
            entity_type="message",
            entity_id=str(message_id),
            action="message.received",
            after_state={
                "conversation_id": conversation_id,
                "message_type": data.message_type,
                "direction": "inbound",
            },
        )

    return InboundReceipt(
        message_id=message_id,
        conversation_id=conversation_id,
        contact_identity_id=contact_id,
        duplicate=False,
    )


def enqueue_outbound_message(
    session: Session,
    *,
    conversation_id: int,
    text_body: str,
    idempotency_key: str,
    ctx: ExecutionContext,
) -> OutboundReceipt:
    """Atomically persist the logical message and its durable delivery job."""
    now = _now()
    retention = timedelta(days=get_settings().message_content_retention_days)
    with session.begin():
        require_permission(session, ctx, DELIVERIES_CREATE)
        # Serialize the organization/key pair before checking it. This avoids
        # creating a second logical Message during concurrent retries.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, :org))"),
            {"key": idempotency_key, "org": ctx.organization_id},
        )
        existing = session.scalar(
            select(OutboundMessage).where(
                OutboundMessage.organization_id == ctx.organization_id,
                OutboundMessage.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.conversation_id != conversation_id
                or existing.payload.get("text") != text_body
            ):
                raise AppError(ErrorCode.IDEMPOTENCY_KEY_REUSED)
            return OutboundReceipt(
                outbound_id=existing.id,
                message_id=existing.message_id,
                conversation_id=existing.conversation_id,
                status=existing.status,
                duplicate=True,
            )

        conversation_row = session.execute(
            select(
                Conversation,
                ChannelAccount.provider,
                ChannelAccount.external_account_id,
                ContactIdentity.external_contact_id,
            )
            .join(
                ChannelAccount,
                and_(
                    ChannelAccount.organization_id == Conversation.organization_id,
                    ChannelAccount.id == Conversation.channel_account_id,
                ),
            )
            .join(
                ContactIdentity,
                and_(
                    ContactIdentity.organization_id == Conversation.organization_id,
                    ContactIdentity.id == Conversation.contact_identity_id,
                ),
            )
            .where(
                Conversation.organization_id == ctx.organization_id,
                Conversation.id == conversation_id,
            )
        ).one_or_none()
        if conversation_row is None:
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found.")
        conversation = conversation_row[0]
        if conversation.status == "closed":
            raise AppError(ErrorCode.ENTITY_INACTIVE, "Conversation is closed.")

        message = Message(
            organization_id=ctx.organization_id,
            channel_account_id=conversation.channel_account_id,
            conversation_id=conversation.id,
            direction="outbound",
            provider_message_id=None,
            message_type="text",
            body_text=text_body,
            media_reference=None,
            delivery_status="pending",
            occurred_at=now,
            content_expires_at=now + retention,
        )
        session.add(message)
        session.flush()
        payload = {
            "schema_version": "1.0",
            "provider": conversation_row.provider,
            "channel_account_external_id": conversation_row.external_account_id,
            "external_contact_id": conversation_row.external_contact_id,
            "message_type": "text",
            "text": text_body,
            "message_id": message.id,
        }
        outbound = OutboundMessage(
            organization_id=ctx.organization_id,
            conversation_id=conversation.id,
            message_id=message.id,
            idempotency_key=idempotency_key,
            payload=payload,
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
        )
        session.add(outbound)
        session.flush()
        record_event(
            session,
            ctx=ctx,
            entity_type="outbound_message",
            entity_id=str(outbound.id),
            action="outbound.queued",
            after_state={"conversation_id": conversation.id, "status": "pending"},
        )

    return OutboundReceipt(
        outbound_id=outbound.id,
        message_id=message.id,
        conversation_id=conversation.id,
        status=outbound.status,
        duplicate=False,
    )


def claim_outbound_messages(
    session: Session,
    *,
    limit: int,
    provider: str | None = None,
    ctx: ExecutionContext,
) -> list[OutboundDispatchItem]:
    """Lease due rows with ``SKIP LOCKED`` so multiple workers never duplicate a claim."""
    if provider is not None and provider not in DISPATCHABLE_PROVIDERS:
        raise AppError(ErrorCode.INVALID_INPUT, "The outbound provider is not dispatchable.")
    now = _now()
    with session.begin():
        require_permission(session, ctx, DELIVERIES_MANAGE)
        # A worker may disappear after claiming its final permitted attempt.
        # Once that lease expires there is no safe fourth delivery attempt, so
        # close it deterministically instead of leaving it stuck forever.
        exhausted = list(
            session.scalars(
                select(OutboundMessage)
                .where(
                    OutboundMessage.organization_id == ctx.organization_id,
                    OutboundMessage.status == "processing",
                    OutboundMessage.next_attempt_at <= now,
                    OutboundMessage.attempt_count >= MAX_OUTBOUND_ATTEMPTS,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for outbound in exhausted:
            outbound.status = "dead_letter"
            outbound.last_error_code = "PROCESSING_LEASE_EXPIRED"
            outbound.updated_at = now
            message = session.get(Message, outbound.message_id)
            message.delivery_status = "dead_letter"
            record_event(
                session,
                ctx=ctx,
                entity_type="outbound_message",
                entity_id=str(outbound.id),
                action="outbound.dead_lettered",
                before_state={"status": "processing"},
                after_state={
                    "status": "dead_letter",
                    "attempt_count": outbound.attempt_count,
                    "error_code": outbound.last_error_code,
                },
            )

        due_statement = (
            select(OutboundMessage)
                .join(
                    Conversation,
                    and_(
                        Conversation.organization_id
                        == OutboundMessage.organization_id,
                        Conversation.id == OutboundMessage.conversation_id,
                    ),
                )
                .join(
                    ChannelAccount,
                    and_(
                        ChannelAccount.organization_id
                        == Conversation.organization_id,
                        ChannelAccount.id == Conversation.channel_account_id,
                    ),
                )
                .where(
                    OutboundMessage.organization_id == ctx.organization_id,
                    ChannelAccount.provider.in_(DISPATCHABLE_PROVIDERS),
                    OutboundMessage.next_attempt_at <= now,
                    or_(
                        OutboundMessage.status.in_(("pending", "failed")),
                        OutboundMessage.status == "processing",
                    ),
                    OutboundMessage.attempt_count < MAX_OUTBOUND_ATTEMPTS,
                )
        )
        if provider is not None:
            due_statement = due_statement.where(ChannelAccount.provider == provider)

        due = list(
            session.scalars(
                due_statement
                .order_by(OutboundMessage.next_attempt_at, OutboundMessage.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        items = []
        for outbound in due:
            outbound.status = "processing"
            outbound.attempt_count += 1
            outbound.next_attempt_at = now + PROCESSING_LEASE
            outbound.updated_at = now
            message = session.get(Message, outbound.message_id)
            message.delivery_status = "processing"
            items.append(
                OutboundDispatchItem(
                    outbound_id=outbound.id,
                    conversation_id=outbound.conversation_id,
                    idempotency_key=outbound.idempotency_key,
                    payload=outbound.payload,
                    attempt_count=outbound.attempt_count,
                )
            )
    return items


def _sandbox_payload_sha256(payload: dict) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def receive_sandbox_message(
    session: Session,
    *,
    outbound_id: int,
    payload: SandboxOutboundPayload,
    ctx: ExecutionContext,
) -> SandboxDeliveryReceiptRead:
    """Record one authenticated, exact-payload local sandbox receipt.

    The outbound row is locked before the receipt is read or created. This
    makes a repeated or concurrent receiver call resolve to the same durable
    receipt rather than creating a second local delivery claim.
    """
    payload_data = payload.model_dump(mode="json")
    payload_sha256 = _sandbox_payload_sha256(payload_data)
    with session.begin():
        require_permission(session, ctx, DELIVERIES_MANAGE)
        outbound_row = session.execute(
            select(OutboundMessage, ChannelAccount.provider)
            .join(
                Conversation,
                and_(
                    Conversation.organization_id == OutboundMessage.organization_id,
                    Conversation.id == OutboundMessage.conversation_id,
                ),
            )
            .join(
                ChannelAccount,
                and_(
                    ChannelAccount.organization_id == Conversation.organization_id,
                    ChannelAccount.id == Conversation.channel_account_id,
                ),
            )
            .where(
                OutboundMessage.organization_id == ctx.organization_id,
                OutboundMessage.id == outbound_id,
            )
            .with_for_update()
        ).one_or_none()
        if outbound_row is None:
            raise AppError(ErrorCode.NOT_FOUND, "Outbound message not found.")
        outbound, provider = outbound_row
        if provider != "sandbox":
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "Only sandbox outbound messages can be received locally.",
            )

        receipt = session.scalar(
            select(SandboxDeliveryReceipt)
            .where(
                SandboxDeliveryReceipt.organization_id == ctx.organization_id,
                SandboxDeliveryReceipt.outbound_id == outbound.id,
            )
            .with_for_update()
        )
        if receipt is not None:
            if receipt.payload_sha256 != payload_sha256:
                raise AppError(
                    ErrorCode.INVALID_INPUT,
                    "The sandbox receipt payload does not match the original outbound message.",
                )
            return SandboxDeliveryReceiptRead(
                outbound_id=outbound.id,
                provider_message_id=receipt.provider_message_id,
                duplicate=True,
            )
        if outbound.status != "processing":
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "Only a processing sandbox outbound message can be received.",
            )
        if outbound.payload != payload_data:
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "The sandbox payload does not match the original outbound message.",
            )

        provider_message_id = f"sandbox-{outbound.id}"
        session.add(
            SandboxDeliveryReceipt(
                organization_id=ctx.organization_id,
                outbound_id=outbound.id,
                provider_message_id=provider_message_id,
                payload_sha256=payload_sha256,
            )
        )
        session.flush()
        record_event(
            session,
            ctx=ctx,
            entity_type="outbound_message",
            entity_id=str(outbound.id),
            action="outbound.sandbox.received",
            before_state={"status": outbound.status},
            after_state={
                "status": outbound.status,
                "provider": "sandbox",
                "provider_message_id": provider_message_id,
            },
        )
        return SandboxDeliveryReceiptRead(
            outbound_id=outbound.id,
            provider_message_id=provider_message_id,
            duplicate=False,
        )


def settle_outbound_result(
    session: Session,
    *,
    outbound_id: int,
    data: OutboundResultCreate,
    result_idempotency_key: str,
    ctx: ExecutionContext,
) -> OutboundStatusRead:
    now = _now()
    with session.begin():
        require_permission(session, ctx, DELIVERIES_MANAGE)
        outbound = session.scalar(
            select(OutboundMessage)
            .where(
                OutboundMessage.organization_id == ctx.organization_id,
                OutboundMessage.id == outbound_id,
            )
            .with_for_update()
        )
        if outbound is None:
            raise AppError(ErrorCode.NOT_FOUND, "Outbound message not found.")
        if outbound.last_result_idempotency_key == result_idempotency_key:
            return OutboundStatusRead(
                outbound_id=outbound.id,
                status=outbound.status,
                attempt_count=outbound.attempt_count,
                next_attempt_at=outbound.next_attempt_at,
            )
        if outbound.status != "processing":
            raise AppError(
                ErrorCode.INVALID_INPUT,
                "Only a processing outbound message can be settled.",
            )

        provider = session.scalar(
            select(ChannelAccount.provider)
            .select_from(Conversation)
            .join(
                ChannelAccount,
                and_(
                    ChannelAccount.organization_id == Conversation.organization_id,
                    ChannelAccount.id == Conversation.channel_account_id,
                ),
            )
            .where(
                Conversation.organization_id == ctx.organization_id,
                Conversation.id == outbound.conversation_id,
            )
        )
        if provider is None:
            raise AppError(ErrorCode.NOT_FOUND, "Outbound message not found.")

        message = session.get(Message, outbound.message_id)
        if data.outcome in {"sent", "delivered"}:
            if provider == "sandbox":
                receipt = session.scalar(
                    select(SandboxDeliveryReceipt)
                    .where(
                        SandboxDeliveryReceipt.organization_id == ctx.organization_id,
                        SandboxDeliveryReceipt.outbound_id == outbound.id,
                    )
                    .with_for_update()
                )
                if (
                    receipt is None
                    or receipt.provider_message_id != data.provider_message_id
                ):
                    raise AppError(
                        ErrorCode.INVALID_INPUT,
                        "Sandbox delivery requires a matching local receipt.",
                    )
            outbound.status = data.outcome
            outbound.provider_message_id = data.provider_message_id
            outbound.last_error_code = None
            outbound.next_attempt_at = now
            message.provider_message_id = data.provider_message_id
            message.delivery_status = data.outcome
        elif data.outcome == "permanent_failure" or (
            data.outcome == "transient_failure"
            and outbound.attempt_count >= MAX_OUTBOUND_ATTEMPTS
        ):
            outbound.status = "dead_letter"
            outbound.last_error_code = data.error_code
            outbound.next_attempt_at = now
            message.delivery_status = "dead_letter"
        else:
            outbound.status = "failed"
            outbound.last_error_code = data.error_code
            delay_minutes = min(2**outbound.attempt_count, 15)
            outbound.next_attempt_at = now + timedelta(minutes=delay_minutes)
            message.delivery_status = "failed"

        outbound.last_result_idempotency_key = result_idempotency_key
        outbound.updated_at = now
        record_event(
            session,
            ctx=ctx,
            entity_type="outbound_message",
            entity_id=str(outbound.id),
            action="outbound.settled",
            before_state={"status": "processing"},
            after_state={
                "status": outbound.status,
                "attempt_count": outbound.attempt_count,
                "error_code": outbound.last_error_code,
            },
        )

    return OutboundStatusRead(
        outbound_id=outbound.id,
        status=outbound.status,
        attempt_count=outbound.attempt_count,
        next_attempt_at=outbound.next_attempt_at,
    )


def redact_expired_message_content(
    session: Session,
    *,
    organization_id: int,
    now: datetime | None = None,
    limit: int = 500,
) -> int:
    """Redact one tenant's expired content while preserving delivery metadata.

    The organization is intentionally required at the service boundary. A
    maintenance command that omits it must fail before it can scan or mutate
    another tenant's messages.
    """
    instant = now or _now()
    ids = list(
        session.scalars(
            select(Message.id)
            .where(
                Message.organization_id == organization_id,
                Message.content_expires_at <= instant,
                Message.content_redacted_at.is_(None),
            )
            .order_by(Message.id)
            .limit(limit)
        )
    )
    if not ids:
        return 0
    session.execute(
        update(Message)
        .where(
            Message.organization_id == organization_id,
            Message.id.in_(ids),
        )
        .values(body_text=None, media_reference=None, content_redacted_at=instant)
    )
    session.commit()
    return len(ids)


# --- B3 staff reads: conversations, messages, handoffs, claim ------------------
#
# Spec: ``docs/superpowers/specs/2026-10-01-erp-b3.md``. Human principals only
# (agents keep ``/internal/*`` and their tools); every read is org-scoped and
# authorized inside ``session.begin()``; message content respects retention.

PREVIEW_CHARS = 80
OP_HANDOFF_CLAIM = "reception_handoff.claim"
HANDOFF_ENTITY_TYPE = "reception_handoff"
MEDIA_TYPES = ("audio", "image")


class HandoffErrorCode(str, Enum):
    HANDOFF_NOT_PENDING = "HANDOFF_NOT_PENDING"


def _handoff_not_pending(status: str) -> AppError:
    return AppError(
        HandoffErrorCode.HANDOFF_NOT_PENDING,
        "The handoff is no longer pending.",
        details={"status": status},
        http_status=409,
    )


ContentState = Literal["available", "redacted", "expired"]


class MessagePreview(BaseModel):
    direction: str
    text: str | None
    occurred_at: datetime


class ConversationSummary(BaseModel):
    id: int
    status: str
    contact_identity_id: int
    contact_display_name: str
    assigned_principal_id: int | None
    assigned_display_name: str | None
    last_message_at: datetime
    last_message_preview: MessagePreview | None
    pending_handoff_id: int | None


class ConversationPage(BaseModel):
    items: list[ConversationSummary]
    next_cursor: str | None


class StaffMessageRead(BaseModel):
    id: int
    direction: str
    message_type: str
    text: str | None
    has_media: bool
    content_state: ContentState
    delivery_status: str
    occurred_at: datetime


class MessagePage(BaseModel):
    items: list[StaffMessageRead]
    next_cursor: str | None


class HandoffRead(BaseModel):
    id: int
    conversation_id: int
    contact_display_name: str
    reason_code: str
    reason_summary: str
    status: str
    claimed_by_principal_id: int | None
    claimed_by_display_name: str | None
    created_at: datetime
    updated_at: datetime


class HandoffPage(BaseModel):
    items: list[HandoffRead]
    next_cursor: str | None


def mask_phone(e164: str) -> str:
    """``+`` and every digit masked except the last 3; country code not parsed."""
    digits = e164.lstrip("+")
    return "+" + "\u2022" * max(len(digits) - 3, 0) + digits[-3:]


def _display_name(patient_name: str | None, lead_name: str | None, phone: str) -> str:
    return patient_name or lead_name or mask_phone(phone)


def _content_state(redacted_at, expires_at, now: datetime) -> ContentState:
    if redacted_at is not None:
        return "redacted"
    if expires_at <= now:
        return "expired"
    return "available"


def _contact_names():
    """``(Patient, Lead)`` columns joined tenant-consistently to the contact."""
    from app.clinical.models import Patient
    from app.commercial.models import Lead

    return Patient, Lead


def _staff_read(session: Session, ctx: ExecutionContext) -> None:
    from app.observability.common import require_human

    require_human(ctx)
    require_permission(session, ctx, CONVERSATIONS_READ)


def _ts(value: datetime):
    return literal(value, DateTime(timezone=True))


def list_staff_conversations(
    session: Session,
    *,
    ctx: ExecutionContext,
    status: str | None = None,
    location_id: int | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> ConversationPage:
    """Newest activity first, keyset on ``(last_message_at, id) DESC``.

    ``last_message_at`` is mutable: a bumped conversation moves above the
    cursor (never duplicated; seen on refresh). See spec *Cursor guarantee*.
    """
    from app.observability.common import decode_cursor, encode_cursor
    from app.scheduling.models import Appointment

    after = decode_cursor(cursor, (datetime, int)) if cursor else None
    Patient, Lead = _contact_names()
    assignee = aliased(Principal)
    org = ctx.organization_id
    latest = (
        select(
            Message.direction,
            Message.body_text,
            Message.occurred_at,
            Message.content_redacted_at,
            Message.content_expires_at,
        )
        .where(Message.organization_id == org, Message.conversation_id == Conversation.id)
        .order_by(Message.occurred_at.desc(), Message.id.desc())
        .limit(1)
        .correlate(Conversation)
        .lateral("latest")
    )
    pending = (
        select(ReceptionHandoff.id)
        .where(
            ReceptionHandoff.organization_id == org,
            ReceptionHandoff.conversation_id == Conversation.id,
            ReceptionHandoff.status == "pending",
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    statement = (
        select(
            Conversation,
            ContactIdentity.normalized_phone_e164,
            Patient.full_name,
            Lead.full_name,
            assignee.display_name,
            latest.c.direction,
            latest.c.body_text,
            latest.c.occurred_at,
            latest.c.content_redacted_at,
            latest.c.content_expires_at,
            pending.label("pending_handoff_id"),
        )
        .join(
            ContactIdentity,
            and_(
                ContactIdentity.organization_id == Conversation.organization_id,
                ContactIdentity.id == Conversation.contact_identity_id,
            ),
        )
        .outerjoin(
            Patient,
            and_(
                Patient.organization_id == ContactIdentity.organization_id,
                Patient.id == ContactIdentity.patient_id,
            ),
        )
        .outerjoin(
            Lead,
            and_(
                Lead.organization_id == ContactIdentity.organization_id,
                Lead.id == ContactIdentity.lead_id,
            ),
        )
        .outerjoin(assignee, assignee.id == Conversation.assigned_principal_id)
        .outerjoin(latest, true())
        .where(Conversation.organization_id == org)
    )
    if status is not None:
        statement = statement.where(Conversation.status == status)
    if location_id is not None:
        statement = statement.where(
            exists().where(
                Appointment.organization_id == org,
                Appointment.location_id == location_id,
                or_(
                    and_(
                        ContactIdentity.lead_id.is_not(None),
                        Appointment.lead_id == ContactIdentity.lead_id,
                    ),
                    and_(
                        ContactIdentity.patient_id.is_not(None),
                        Appointment.patient_id == ContactIdentity.patient_id,
                    ),
                ),
            )
        )
    if after is not None:
        statement = statement.where(
            tuple_(Conversation.last_message_at, Conversation.id)
            < tuple_(_ts(after[0]), literal(after[1], Integer))
        )
    statement = statement.order_by(
        Conversation.last_message_at.desc(), Conversation.id.desc()
    ).limit(limit + 1)
    with session.begin():
        _staff_read(session, ctx)
        rows = session.execute(statement).all()
        now = _now()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for (
            conversation, phone, patient_name, lead_name, assignee_name,
            direction, body, occurred_at, redacted_at, expires_at, pending_id,
        ) in rows:
            preview = None
            if occurred_at is not None:
                visible = _content_state(redacted_at, expires_at, now) == "available"
                preview = MessagePreview(
                    direction=direction,
                    text=body[:PREVIEW_CHARS] if visible and body is not None else None,
                    occurred_at=occurred_at,
                )
            items.append(
                ConversationSummary(
                    id=conversation.id,
                    status=conversation.status,
                    contact_identity_id=conversation.contact_identity_id,
                    contact_display_name=_display_name(patient_name, lead_name, phone),
                    assigned_principal_id=conversation.assigned_principal_id,
                    assigned_display_name=assignee_name,
                    last_message_at=conversation.last_message_at,
                    last_message_preview=preview,
                    pending_handoff_id=pending_id,
                )
            )
    last = rows[-1][0] if rows else None
    return ConversationPage(
        items=items,
        next_cursor=encode_cursor(last.last_message_at, last.id) if has_more else None,
    )


def list_conversation_messages(
    session: Session,
    *,
    ctx: ExecutionContext,
    conversation_id: int,
    limit: int = 25,
    cursor: str | None = None,
) -> MessagePage:
    """Oldest → newest, keyset on ``(occurred_at, id) ASC``; 404 outside the org."""
    from app.observability.common import decode_cursor, encode_cursor

    after = decode_cursor(cursor, (datetime, int)) if cursor else None
    org = ctx.organization_id
    statement = select(Message).where(
        Message.organization_id == org, Message.conversation_id == conversation_id
    )
    if after is not None:
        statement = statement.where(
            tuple_(Message.occurred_at, Message.id)
            > tuple_(_ts(after[0]), literal(after[1], Integer))
        )
    statement = statement.order_by(Message.occurred_at.asc(), Message.id.asc()).limit(limit + 1)
    with session.begin():
        _staff_read(session, ctx)
        found = session.scalar(
            select(Conversation.id).where(
                Conversation.organization_id == org, Conversation.id == conversation_id
            )
        )
        if found is None:
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found.")
        rows = session.scalars(statement).all()
        now = _now()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for message in rows:
            state = _content_state(message.content_redacted_at, message.content_expires_at, now)
            items.append(
                StaffMessageRead(
                    id=message.id,
                    direction=message.direction,
                    message_type=message.message_type,
                    text=message.body_text if state == "available" else None,
                    has_media=message.message_type in MEDIA_TYPES,
                    content_state=state,
                    delivery_status=message.delivery_status,
                    occurred_at=message.occurred_at,
                )
            )
    last = rows[-1] if rows else None
    return MessagePage(
        items=items,
        next_cursor=encode_cursor(last.occurred_at, last.id) if has_more else None,
    )


def _handoff_statement(org: int):
    Patient, Lead = _contact_names()
    claimant = aliased(Principal)
    return (
        select(
            ReceptionHandoff,
            Conversation.assigned_principal_id,
            claimant.display_name,
            ContactIdentity.normalized_phone_e164,
            Patient.full_name,
            Lead.full_name,
        )
        .join(
            Conversation,
            and_(
                Conversation.organization_id == ReceptionHandoff.organization_id,
                Conversation.id == ReceptionHandoff.conversation_id,
            ),
        )
        .join(
            ContactIdentity,
            and_(
                ContactIdentity.organization_id == ReceptionHandoff.organization_id,
                ContactIdentity.id == ReceptionHandoff.contact_identity_id,
            ),
        )
        .outerjoin(
            Patient,
            and_(
                Patient.organization_id == ContactIdentity.organization_id,
                Patient.id == ContactIdentity.patient_id,
            ),
        )
        .outerjoin(
            Lead,
            and_(
                Lead.organization_id == ContactIdentity.organization_id,
                Lead.id == ContactIdentity.lead_id,
            ),
        )
        .outerjoin(claimant, claimant.id == Conversation.assigned_principal_id)
        .where(ReceptionHandoff.organization_id == org)
    )


def _handoff_read(row) -> HandoffRead:
    handoff, assignee_id, assignee_name, phone, patient_name, lead_name = row
    # No claimant column: derived from the conversation assignee only while
    # the handoff is ``claimed`` (resume never clears the assignee).
    claimed = handoff.status == "claimed"
    return HandoffRead(
        id=handoff.id,
        conversation_id=handoff.conversation_id,
        contact_display_name=_display_name(patient_name, lead_name, phone),
        reason_code=handoff.reason_code,
        reason_summary=handoff.reason_summary,
        status=handoff.status,
        claimed_by_principal_id=assignee_id if claimed else None,
        claimed_by_display_name=assignee_name if claimed else None,
        created_at=handoff.created_at,
        updated_at=handoff.updated_at,
    )


def list_handoffs(
    session: Session,
    *,
    ctx: ExecutionContext,
    status: str = "pending",
    limit: int = 25,
    cursor: str | None = None,
) -> HandoffPage:
    """The handoff queue, oldest first: keyset on ``(created_at, id) ASC``."""
    from app.observability.common import decode_cursor, encode_cursor

    after = decode_cursor(cursor, (datetime, int)) if cursor else None
    statement = _handoff_statement(ctx.organization_id).where(ReceptionHandoff.status == status)
    if after is not None:
        statement = statement.where(
            tuple_(ReceptionHandoff.created_at, ReceptionHandoff.id)
            > tuple_(_ts(after[0]), literal(after[1], Integer))
        )
    statement = statement.order_by(
        ReceptionHandoff.created_at.asc(), ReceptionHandoff.id.asc()
    ).limit(limit + 1)
    with session.begin():
        _staff_read(session, ctx)
        rows = session.execute(statement).all()
        has_more = len(rows) > limit
        items = [_handoff_read(row) for row in rows[:limit]]
    last = items[-1] if items else None
    return HandoffPage(
        items=items,
        next_cursor=encode_cursor(last.created_at, last.id) if has_more else None,
    )


def get_handoff(session: Session, *, ctx: ExecutionContext, handoff_id: int) -> HandoffRead:
    session.expire_all()
    with session.begin():
        _staff_read(session, ctx)
        row = session.execute(
            _handoff_statement(ctx.organization_id).where(ReceptionHandoff.id == handoff_id)
        ).first()
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "Handoff not found.")
        return _handoff_read(row)


def claim_handoff(
    session: Session,
    *,
    ctx: ExecutionContext,
    handoff_id: int,
    idempotency: IdempotencyClaim | None = None,
) -> int:
    """A human takes over a pending handoff: one tx, receipt first, row locks."""
    from app.observability.common import require_human

    now = _now()
    with session.begin():
        receipt = claim_receipt(session, ctx, idempotency)
        require_human(ctx)
        require_permission(session, ctx, CONVERSATIONS_RESUME)
        handoff = session.scalar(
            select(ReceptionHandoff)
            .where(
                ReceptionHandoff.organization_id == ctx.organization_id,
                ReceptionHandoff.id == handoff_id,
            )
            .with_for_update()
        )
        if handoff is None:
            raise AppError(ErrorCode.NOT_FOUND, "Handoff not found.")
        conversation = session.scalar(
            select(Conversation)
            .where(
                Conversation.organization_id == ctx.organization_id,
                Conversation.id == handoff.conversation_id,
            )
            .with_for_update()
        )
        mine = (
            handoff.status == "claimed"
            and conversation.assigned_principal_id == ctx.principal_id
        )
        if not mine:
            if handoff.status != "pending":
                raise _handoff_not_pending(handoff.status)
            handoff.status = "claimed"
            handoff.updated_at = now
            conversation.assigned_principal_id = ctx.principal_id
            conversation.updated_at = now
            session.flush()
            record_event(
                session,
                ctx=ctx,
                entity_type=HANDOFF_ENTITY_TYPE,
                entity_id=str(handoff.id),
                action="reception_handoff.claimed",
                before_state={"status": "pending"},
                after_state={
                    "status": "claimed",
                    "conversation_id": conversation.id,
                    "assigned_principal_id": ctx.principal_id,
                },
            )
        settle_receipt(
            receipt,
            resource_type=HANDOFF_ENTITY_TYPE,
            resource_id=str(handoff.id),
            outcome_json={"handoff_id": handoff.id},
        )
        return handoff.id
