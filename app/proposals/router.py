"""B2 HTTP surface: agents propose, humans approve or decline, one inbox.

Thin by contract: HTTP shape → schema → service → ``InboxItem``. Mounted in the
authenticated loop of ``app/__init__.py``; ``resolve_http_context`` reuses the
credential-backed context cached on ``request.state``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.context import resolve_http_context
from app.db import get_db
from app.messaging.router import require_uuid4_idempotency_key
from app.proposals.schemas import (
    InboxItem,
    InboxKind,
    InboxPage,
    InboxSource,
    InboxStatus,
    ProposalApprove,
    ProposalCreate,
    ProposalDecline,
)
from app.proposals.service import (
    approve_and_execute,
    decline_proposal,
    get_proposal_item,
    list_inbox,
    proposal_item,
    submit_proposal,
)

router = APIRouter(prefix="/agent", tags=["agent-proposals"])
REPLAY_HEADER = "Idempotent-Replay"


@router.post("/proposals", response_model=InboxItem, status_code=201)
def create_proposal_route(
    payload: ProposalCreate,
    request: Request,
    response: Response,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> InboxItem:
    ctx = resolve_http_context(request)
    result = submit_proposal(
        db,
        ctx=ctx,
        kind=payload.kind,
        payload=payload.payload,
        reason=payload.reason,
        evidence=payload.evidence,
        key=idempotency_key,
    )
    if not result.created:
        response.status_code = 200
    if result.replayed:
        response.headers[REPLAY_HEADER] = "true"
    return proposal_item(db, ctx, result.proposal_id)


@router.get("/inbox", response_model=InboxPage)
def inbox_route(
    request: Request,
    status: InboxStatus = "pending",
    source: InboxSource | None = None,
    kind: InboxKind | None = None,
    location_id: int | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    db: Session = Depends(get_db),
) -> InboxPage:
    ctx = resolve_http_context(request)
    return list_inbox(
        db,
        ctx=ctx,
        status=status,
        source=source,
        kind=kind,
        location_id=location_id,
        limit=limit,
        cursor=cursor,
    )


@router.get("/proposals/{proposal_id}", response_model=InboxItem)
def get_proposal_route(
    proposal_id: int, request: Request, db: Session = Depends(get_db)
) -> InboxItem:
    ctx = resolve_http_context(request)
    return get_proposal_item(db, ctx=ctx, proposal_id=proposal_id)


@router.post("/proposals/{proposal_id}/approve", response_model=InboxItem)
def approve_proposal_route(
    proposal_id: int,
    payload: ProposalApprove,
    request: Request,
    response: Response,
    idempotency_key: str = Depends(require_uuid4_idempotency_key),
    db: Session = Depends(get_db),
) -> InboxItem:
    ctx = resolve_http_context(request)
    result = approve_and_execute(
        db,
        ctx=ctx,
        proposal_id=proposal_id,
        payload_hash=payload.payload_hash,
        note=payload.note,
        key=idempotency_key,
    )
    if result.replayed:
        response.headers[REPLAY_HEADER] = "true"
    return proposal_item(db, ctx, proposal_id)


@router.post("/proposals/{proposal_id}/decline", response_model=InboxItem)
def decline_proposal_route(
    proposal_id: int,
    request: Request,
    payload: ProposalDecline | None = None,
    db: Session = Depends(get_db),
) -> InboxItem:
    ctx = resolve_http_context(request)
    decline_proposal(
        db, ctx=ctx, proposal_id=proposal_id, note=payload.note if payload else None
    )
    return proposal_item(db, ctx, proposal_id)
