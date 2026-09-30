"""COB error code, rendered through the stable envelope (``ProposalErrorCode`` pattern).

``app/errors.py`` is protected, so the code is built with an explicit message
and HTTP status; the registered handler renders ``code.value``.
"""

from __future__ import annotations

from enum import Enum

from app.errors import AppError


class AgentRunErrorCode(str, Enum):
    AGENT_DISABLED = "AGENT_DISABLED"


#: ``details.reason``: the kill switch is off, or the org's proposer is missing.
DISABLED = "disabled"
NOT_PROVISIONED = "not_provisioned"


def agent_disabled(agent_key: str, reason: str) -> AppError:
    return AppError(
        AgentRunErrorCode.AGENT_DISABLED,
        "The agent is disabled for this organization.",
        details={"agent_key": agent_key, "reason": reason},
        http_status=409,
    )
