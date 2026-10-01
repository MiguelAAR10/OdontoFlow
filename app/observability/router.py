"""B3 HTTP surface: ``GET /activity`` and ``GET /metrics/productivity``.

Thin by contract: query model (``extra="forbid"``) → service → typed response.
Mounted in the authenticated loop of ``app/__init__.py``; human principals only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.context import resolve_http_context
from app.db import get_db
from app.observability.activity import list_activity
from app.observability.metrics import productivity
from app.observability.schemas import (
    ActivityPage,
    ActivityQuery,
    ProductivityQuery,
    ProductivityReport,
)

router = APIRouter(tags=["staff-observability"])


@router.get("/activity", response_model=ActivityPage)
def activity_route(
    request: Request,
    query: Annotated[ActivityQuery, Query()],
    db: Session = Depends(get_db),
) -> ActivityPage:
    return list_activity(
        db,
        ctx=resolve_http_context(request),
        location_id=query.location_id,
        agent_key=query.agent_key,
        since=query.since,
        limit=query.limit,
        cursor=query.cursor,
    )


@router.get(
    "/metrics/productivity",
    response_model=ProductivityReport,
    response_model_by_alias=True,
)
def productivity_route(
    request: Request,
    query: Annotated[ProductivityQuery, Query()],
    db: Session = Depends(get_db),
) -> ProductivityReport:
    return productivity(
        db,
        ctx=resolve_http_context(request),
        start=query.from_,
        end=query.to,
        location_id=query.location_id,
    )
