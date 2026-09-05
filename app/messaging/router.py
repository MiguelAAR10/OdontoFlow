from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.orm import Session

from app.context import resolve_http_context
from app.db import get_db
from app.errors import AppError, ErrorCode
from app.agent_tools.reception import resume_automation
from app.agent_tools.schemas import EmptyArguments
from app.idempotency.service import run_idempotent_command
from app.messaging.schemas import (
    InboundMessageCreate,
    InboundReceipt,
    ConversationCloseReceipt,
    ConversationRead,
    ConversationStatus,
    OutboundClaimRequest,
    OutboundDispatchItem,
    OutboundMessageCreate,
    OutboundReceipt,
    OutboundResultCreate,
    OutboundStatusRead,
    ResumeAutomationReceipt,
    ResumeAutomationRequest,
)
from app.messaging.service import (
    claim_outbound_messages,
    close_conversation,
    enqueue_outbound_message,
    ingest_inbound_message,
    list_conversations,
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
        ctx=resolve_http_context(request),
    )


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
