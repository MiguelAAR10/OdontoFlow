"""Shared conversation-state guards for the LLM-facing gateway."""

from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.messaging.models import Conversation


def require_automation_active(conversation: Conversation) -> None:
    if conversation.status == "human_handoff":
        raise AppError(
            ErrorCode.ENTITY_INACTIVE,
            "Conversation is assigned to human reception.",
            details={"reason": "HUMAN_HANDOFF_ACTIVE"},
        )


def require_human_confirmation(ctx: ExecutionContext) -> None:
    """Refuse agent confirmations before commands or stored results are reached.

    A later inbound message does not prove verified patient acceptance. This
    shared rule applies to booking, cancellation, and rescheduling confirmations.
    """
    if ctx.principal_type == "agent":
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "Automatic appointment confirmation requires verified patient acceptance.",
        )
