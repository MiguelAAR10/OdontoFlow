"""Shared conversation-state guards for the LLM-facing gateway."""

from app.errors import AppError, ErrorCode
from app.messaging.models import Conversation


def require_automation_active(conversation: Conversation) -> None:
    if conversation.status == "human_handoff":
        raise AppError(
            ErrorCode.ENTITY_INACTIVE,
            "Conversation is assigned to human reception.",
            details={"reason": "HUMAN_HANDOFF_ACTIVE"},
        )
