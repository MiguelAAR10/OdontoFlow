from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.agent_tools.schemas import AgentToolCall, AgentToolCatalog, AgentToolResult
from app.agent_tools.service import call_agent_tool, list_agent_tools
from app.context import resolve_http_context
from app.db import get_db

router = APIRouter(prefix="/agent-tools", tags=["agent-tools"])


@router.post("/call", response_model=AgentToolResult)
def call_tool_route(
    payload: AgentToolCall,
    request: Request,
    db: Session = Depends(get_db),
) -> AgentToolResult:
    return call_agent_tool(
        db,
        call=payload,
        ctx=resolve_http_context(request),
    )


@router.get("/catalog", response_model=AgentToolCatalog)
def catalog_route(
    request: Request,
    db: Session = Depends(get_db),
) -> AgentToolCatalog:
    return list_agent_tools(db, ctx=resolve_http_context(request))
