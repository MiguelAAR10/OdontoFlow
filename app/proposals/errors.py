"""B2 proposal error codes, rendered through the existing stable envelope.

Same pattern as :class:`app.iam.service.IamErrorCode`: ``app/errors.py`` is a
protected surface, so each code is always built with an explicit message and
HTTP status, and the registered handler renders ``code.value``.
"""

from __future__ import annotations

from enum import Enum

from app.errors import AppError


class ProposalErrorCode(str, Enum):
    PROPOSAL_HASH_MISMATCH = "PROPOSAL_HASH_MISMATCH"
    PROPOSAL_SUPERSEDED = "PROPOSAL_SUPERSEDED"
    PROPOSAL_NOT_PENDING = "PROPOSAL_NOT_PENDING"
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"


_STATUS = {
    ProposalErrorCode.PROPOSAL_HASH_MISMATCH: 409,
    ProposalErrorCode.PROPOSAL_SUPERSEDED: 409,
    ProposalErrorCode.PROPOSAL_NOT_PENDING: 409,
    ProposalErrorCode.PROPOSAL_EXPIRED: 410,
}
_MESSAGE = {
    ProposalErrorCode.PROPOSAL_HASH_MISMATCH: "The approved payload does not match the proposal.",
    ProposalErrorCode.PROPOSAL_SUPERSEDED: "The proposal is out of date: its subject changed.",
    ProposalErrorCode.PROPOSAL_NOT_PENDING: "The proposal is no longer pending.",
    ProposalErrorCode.PROPOSAL_EXPIRED: "The proposal has expired.",
}


def proposal_error(code: ProposalErrorCode, details: dict | None = None) -> AppError:
    return AppError(code, _MESSAGE[code], details=details or {}, http_status=_STATUS[code])


def error_for_status(status: str) -> AppError:
    """The error a decision on a non-pending proposal returns (spec: error by status)."""
    if status == "expired":
        return proposal_error(ProposalErrorCode.PROPOSAL_EXPIRED)
    if status == "superseded":
        return proposal_error(ProposalErrorCode.PROPOSAL_SUPERSEDED)
    return proposal_error(ProposalErrorCode.PROPOSAL_NOT_PENDING, {"status": status})
