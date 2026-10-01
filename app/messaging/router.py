from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agent_tools.reception import resume_automation
from app.agent_tools.schemas import EmptyArguments
from app.context import resolve_http_context
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.idempotency.service import run_idempotent_command
from app.messaging.schemas import (
    ConversationCloseReceipt,
    ConversationRead,
    ConversationStatus,
    InboundMessageCreate,
    InboundReceipt,
    OutboundClaimRequest,
    OutboundDispatchItem,
    OutboundMessageCreate,
    OutboundReceipt,
    OutboundResultCreate,
    OutboundStatusRead,
    ResumeAutomationReceipt,
    ResumeAutomationRequest,
    SandboxDeliveryReceiptRead,
    SandboxDeliveryRequest,
)
from app.messaging.service import (
    OP_HANDOFF_CLAIM,
    ConversationPage,
    HandoffPage,
    HandoffRead,
    MessagePage,
    claim_handoff,
    claim_outbound_messages,
    close_conversation,
    enqueue_outbound_message,
    get_handoff,
    ingest_inbound_message,
    list_conversation_messages,
    list_conversations,
    list_handoffs,
    list_staff_conversations,
    receive_sandbox_message,
    settle_outbound_result,
)

router = APIRouter(prefix="/internal", tags=["integration-messaging"])
UUID4_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CONVERSATIONS_CLOSE_OPERATION = "conversations.close"


def require_uuid4_idempotency_key(
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=36,
        max_length=36,
        pattern=UUID4_PATTERN,
    ),
) -> str:
    try:
        parsed = UUID(idempotency_key)
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != idempotency_key:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Idempotency-Key must be a canonical UUIDv4 value.",
        )
    return idempotency_key


@router.get("/conversations", response_model=list[ConversationRead])
def list_conversations_route(
    request: Request,
    status: ConversationStatus | None = Query(default=None),
    last_message_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[ConversationRead]:
    conversations = list_conversations(
        db,
        ctx=resolve_http_context(request),
        status=status,
        last_message_before=last_message_before,
        limit=limit,
    )
    return [
        ConversationRead(
            conversation_id=conversation.id,
            contact_identity_id=conversation.contact_identity_id,
            status=conversation.status,
            last_message_at=conversation.last_message_at,
        )
        for conversation in conversations
    ]


@router.post("/messages/inbound", response_model=InboundReceipt, status_code=201)
def ingest_inbound_route(
    payload: InboundMessageCreate,
    request: Request,
    response: Response,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> InboundReceipt:
    # ``provider_message_id`` is the authoritative durable dedupe key. The
    # transport UUID is still mandatory so every integration mutation follows
    # the same retry policy and is traceable at the caller.
    del idempotency_key
    receipt = ingest_inbound_message(db, payload, ctx=resolve_http_context(request))
    if receipt.duplicate:
        response.status_code = 200
    return receipt


@router.post(
    "/conversations/{conversation_id}/outbound",
    response_model=OutboundReceipt,
    status_code=201,
)
def enqueue_outbound_route(
    conversation_id: int,
    payload: OutboundMessageCreate,
    request: Request,
    response: Response,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> OutboundReceipt:
    receipt = enqueue_outbound_message(
        db,
        conversation_id=conversation_id,
        text_body=payload.text,
        idempotency_key=idempotency_key,
        ctx=resolve_http_context(request),
    )
    if receipt.duplicate:
        response.status_code = 200
    return receipt


@router.post(
    "/conversations/{conversation_id}/close",
    response_model=ConversationCloseReceipt,
)
def close_conversation_route(
    conversation_id: int,
    request: Request,
    response: Response,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> ConversationCloseReceipt:
    ctx = resolve_http_context(request)
    outcome = run_idempotent_command(
        db,
        operation=close_conversation,
        operation_name=CONVERSATIONS_CLOSE_OPERATION,
        key=idempotency_key,
        ctx=ctx,
        params={"conversation_id": conversation_id},
        conversation_id=conversation_id,
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    if outcome.replayed:
        response.headers["Idempotent-Replay"] = "true"
    return ConversationCloseReceipt(**value, replayed=outcome.replayed)


@router.post("/outbound/claim", response_model=list[OutboundDispatchItem])
def claim_outbound_route(
    payload: OutboundClaimRequest,
    request: Request,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> list[OutboundDispatchItem]:
    del idempotency_key
    return claim_outbound_messages(
        db,
        limit=payload.limit,
        provider=payload.provider,
        ctx=resolve_http_context(request),
    )


@router.post(
    "/sandbox/receive",
    response_model=SandboxDeliveryReceiptRead,
    status_code=201,
)
def receive_sandbox_route(
    payload: SandboxDeliveryRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> SandboxDeliveryReceiptRead:
    del idempotency_key
    receipt = receive_sandbox_message(
        db,
        outbound_id=payload.outbound_id,
        payload=payload.payload,
        ctx=resolve_http_context(request),
    )
    if receipt.duplicate:
        response.status_code = 200
    return receipt


@router.post("/outbound/{outbound_id}/result", response_model=OutboundStatusRead)
def settle_outbound_route(
    outbound_id: int,
    payload: OutboundResultCreate,
    request: Request,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> OutboundStatusRead:
    return settle_outbound_result(
        db,
        outbound_id=outbound_id,
        data=payload,
        result_idempotency_key=idempotency_key,
        ctx=resolve_http_context(request),
    )


@router.post(
    "/conversations/{conversation_id}/resume",
    response_model=ResumeAutomationReceipt,
)
def resume_automation_route(
    conversation_id: int,
    payload: ResumeAutomationRequest,
    request: Request,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> ResumeAutomationReceipt:
    del payload
    ctx = resolve_http_context(request)
    outcome = run_idempotent_command(
        db,
        operation=resume_automation,
        operation_name="conversations.resume_automation",
        key=idempotency_key,
        ctx=ctx,
        params={"conversation_id": conversation_id},
        conversation_id=conversation_id,
        arguments=EmptyArguments(),
    )
    value = outcome.outcome if outcome.replayed else outcome.result
    return ResumeAutomationReceipt(
        conversation_id=int(value["conversation_id"]),
        status="open",
        resolved_handoff_ids=[int(item) for item in value["resolved_handoff_ids"]],
        replayed=outcome.replayed,
    )


# --- B3 staff reads (no prefix; mounted on the authenticated boundary) ---------
#
# Thin by contract: query model (``extra="forbid"``) → service → page. Human
# principals only; the service owns the transaction and the authorization.

staff_router = APIRouter(tags=["staff-conversations"])
REPLAY_HEADER = "Idempotent-Replay"


class _Page(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=512)


class ConversationListQuery(_Page):
    status: ConversationStatus | None = None
    location_id: int | None = Field(default=None, ge=1)


class HandoffListQuery(_Page):
    status: Literal["pending", "claimed", "resolved"] = "pending"


@staff_router.get("/conversations", response_model=ConversationPage)
def staff_conversations_route(
    request: Request,
    query: Annotated[ConversationListQuery, Query()],
    db: Session = Depends(get_db),
) -> ConversationPage:
    return list_staff_conversations(
        db,
        ctx=resolve_http_context(request),
        status=query.status,
        location_id=query.location_id,
        limit=query.limit,
        cursor=query.cursor,
    )


@staff_router.get("/conversations/{conversation_id}/messages", response_model=MessagePage)
def staff_messages_route(
    conversation_id: int,
    request: Request,
    query: Annotated[_Page, Query()],
    db: Session = Depends(get_db),
) -> MessagePage:
    return list_conversation_messages(
        db,
        ctx=resolve_http_context(request),
        conversation_id=conversation_id,
        limit=query.limit,
        cursor=query.cursor,
    )


@staff_router.get("/handoffs", response_model=HandoffPage)
def staff_handoffs_route(
    request: Request,
    query: Annotated[HandoffListQuery, Query()],
    db: Session = Depends(get_db),
) -> HandoffPage:
    return list_handoffs(
        db,
        ctx=resolve_http_context(request),
        status=query.status,
        limit=query.limit,
        cursor=query.cursor,
    )


@staff_router.post("/handoffs/{handoff_id}/claim", response_model=HandoffRead)
def claim_handoff_route(
    handoff_id: int,
    request: Request,
    response: Response,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> HandoffRead:
    ctx = resolve_http_context(request)
    outcome = run_idempotent_command(
        db,
        operation=claim_handoff,
        operation_name=OP_HANDOFF_CLAIM,
        key=idempotency_key,
        ctx=ctx,
        params={"handoff_id": handoff_id},
        handoff_id=handoff_id,
    )
    if outcome.replayed:
        response.headers[REPLAY_HEADER] = "true"
    return get_handoff(db, ctx=ctx, handoff_id=handoff_id)
