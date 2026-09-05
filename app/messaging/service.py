"""Deterministic Phase 2 messaging services and PostgreSQL outbound queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.audit.service import record_event
from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.permissions import DELIVERIES_CREATE, DELIVERIES_MANAGE, MESSAGES_CREATE
from app.iam.service import require_permission
from app.messaging.models import (
    ChannelAccount,
    ContactIdentity,
    Conversation,
    Message,
    OutboundMessage,
)
from app.messaging.schemas import (
    InboundMessageCreate,
    InboundReceipt,
    OutboundDispatchItem,
    OutboundReceipt,
    OutboundResultCreate,
    OutboundStatusRead,
)

UTC = timezone.utc
MAX_OUTBOUND_ATTEMPTS = 3
PROCESSING_LEASE = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(UTC)


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
    ctx: ExecutionContext,
) -> list[OutboundDispatchItem]:
    """Lease due rows with ``SKIP LOCKED`` so multiple workers never duplicate a claim."""
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

        due = list(
            session.scalars(
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
                    ChannelAccount.provider != "test",
                    OutboundMessage.next_attempt_at <= now,
                    or_(
                        OutboundMessage.status.in_(("pending", "failed")),
                        OutboundMessage.status == "processing",
                    ),
                    OutboundMessage.attempt_count < MAX_OUTBOUND_ATTEMPTS,
                )
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

        message = session.get(Message, outbound.message_id)
        if data.outcome in {"sent", "delivered"}:
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
