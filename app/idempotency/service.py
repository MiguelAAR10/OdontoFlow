"""PF4 — the idempotent command handler (PF0 spec §15–§16).

The transport's only job is to pass the ``Idempotency-Key`` header through
(C10); this module is the application-level command handler that implements
the §16.1 in-transaction ordering and the §16.2 collision resolution:

1. **Claim first.** The operation receives an :class:`IdempotencyClaim` and
   stages the ``command_receipts`` row as its first statement, before
   permission evaluation and before any preflight read. A duplicate key
   therefore surfaces as ``23505`` on ``uq_command_receipts_org_operation_key``
   before any expensive work, and a command never holds a GiST or row lock
   while waiting on the receipt index (§16.1 — no new deadlock class).
2. **Execute.** The existing application service owns its transaction exactly
   as before (A2/C9): the claim, the mutation and the audit row land
   together or not at all; the service fills ``resource_id``/``outcome_json``
   before commit.
3. **Resolve collisions.** A receipt conflict rolls back the aborted
   transaction and reads the committed receipt in a *separate* read-only
   transaction (C9). Matching fingerprint and matching principal → REPLAY the
   stored outcome; anything else → ``IDEMPOTENCY_KEY_REUSED`` with no detail
   about the stored payload (C2/C6/I8).

The handler never retries with anything, never polls, never opens a
transaction inside another, and never classifies a non-receipt ``23505`` as an
idempotency event (C7).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.idempotency.models import CommandReceipt

UTC = timezone.utc

#: The operations PF4 wires (I12 — the mechanism is generic, only the wiring
#: is staged). These strings are also the values stored in
#: ``command_receipts.operation``.
OP_APPOINTMENTS_BOOK = "appointments.book"
OP_APPOINTMENTS_RESCHEDULE = "appointments.reschedule"
OP_APPOINTMENTS_CANCEL = "appointments.cancel"

#: The unique constraint name is a contract (C7): ``23505`` is treated as an
#: idempotency event only when the violated constraint is exactly this one.
RECEIPT_CONFLICT_CONSTRAINT = "uq_command_receipts_org_operation_key"
UNIQUE_VIOLATION = "23505"

#: Key requirement policy (I10): agents and integrations retry automatically,
#: so they must always supply a key; humans keep the current contract.
KEY_REQUIRED_PRINCIPAL_TYPES = ("agent", "integration")

IDEMPOTENCY_KEY_REUSED_MESSAGE = (
    "The idempotency key was already used by a different request."
)
IDEMPOTENCY_KEY_FORMAT_MESSAGE = (
    "Agent and integration idempotency keys must be canonical UUIDv4 values."
)


def _is_canonical_uuid4(value: str) -> bool:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """The claim an application service stages as its first statement."""

    operation: str
    key: str
    fingerprint: str


class CommandOutcome(NamedTuple):
    """What the command handler produced: the result and whether it replayed.

    ``result`` is the application service's return value (the executed
    path) or ``None``; ``outcome`` is the stored logical outcome when the
    command was replayed (I5 — the transport renders it into the response
    schema the original call produced).
    """

    result: object
    replayed: bool = False
    outcome: dict | None = None


def claim_receipt(
    session: Session, resolved: ExecutionContext, idempotency: IdempotencyClaim | None
) -> CommandReceipt | None:
    """Stage the idempotency claim as the transaction's first statement (§16.1).

    Shared by every idempotent command service: the claim row is added and
    flushed before anything else, so a duplicate key surfaces as ``23505`` on
    ``uq_command_receipts_org_operation_key`` before permission evaluation,
    preflight reads, row locks or the domain insert — the command never holds
    a domain lock while waiting on the receipt index, and a rolled-back
    command never leaves a claim (I7/C3).
    """
    if idempotency is None:
        return None
    receipt = CommandReceipt(
        organization_id=resolved.organization_id,
        principal_id=resolved.principal_id,
        operation=idempotency.operation,
        idempotency_key=idempotency.key,
        request_fingerprint=idempotency.fingerprint,
        request_id=resolved.request_id,
        correlation_id=resolved.correlation_id,
    )
    session.add(receipt)
    session.flush()
    return receipt


def settle_receipt(
    receipt: CommandReceipt | None,
    *,
    resource_type: str,
    resource_id: str,
    outcome_json: dict,
) -> None:
    """Fill the claim's outcome before commit (§16.1 step 6, I5/I13).

    The receipt row, the mutation and the audit row land in the same
    transaction or not at all, so a committed receipt always carries its
    logical outcome and a rolled-back command leaves no trace.
    """
    if receipt is None:
        return
    receipt.resource_type = resource_type
    receipt.resource_id = resource_id
    receipt.outcome_json = outcome_json


def _normalize(value):
    """Canonical form of one command parameter (I4)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def command_fingerprint(*, operation: str, organization_id: int, params: dict) -> str:
    """sha256 of the canonical command payload (I4).

    ``canonical_json`` covers ``operation``, ``organization_id`` and the
    normalized domain parameters: keys sorted, no insignificant whitespace,
    absent optional fields omitted (never emitted as ``null``), integers as
    JSON numbers, timestamps normalized to UTC ISO-8601 with microsecond
    precision. Transport noise — ``request_id``, ``correlation_id``, the
    idempotency key itself, headers — is excluded by construction: it is
    never part of ``params``.
    """
    canonical = {"operation": operation, "organization_id": organization_id}
    for key in sorted(params):
        value = _normalize(params[key])
        if value is None:
            continue
        canonical[key] = value
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_receipt_conflict(exc: IntegrityError) -> bool:
    """C7: is this ``23505`` the receipt claim, and nothing else?"""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if sqlstate is None:
        diag = getattr(orig, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) if diag is not None else None
    if str(sqlstate) != UNIQUE_VIOLATION:
        return False
    diag = getattr(orig, "diag", None)
    if diag is None:
        return False
    return getattr(diag, "constraint_name", None) == RECEIPT_CONFLICT_CONSTRAINT


def _replay_or_reject(
    session: Session,
    ctx: ExecutionContext,
    operation_name: str,
    key: str,
    fingerprint: str,
) -> CommandOutcome:
    """C9: read the committed receipt in a fresh read-only transaction.

    Runs only after the aborted execution transaction was rolled back — an
    aborted transaction cannot read, so this is a separate transaction, never
    a nested one.
    """
    session.rollback()
    with session.begin():
        receipt = session.scalar(
            select(CommandReceipt).where(
                CommandReceipt.organization_id == ctx.organization_id,
                CommandReceipt.operation == operation_name,
                CommandReceipt.idempotency_key == key,
            )
        )
    if (
        receipt is None
        or receipt.request_fingerprint != fingerprint
        or receipt.principal_id != ctx.principal_id
        or receipt.outcome_json is None
    ):
        # C2/C6: deterministic rejection with zero detail about the stored
        # request (I8). A committed receipt always carries an outcome (I13),
        # so a None outcome is treated as a foreign request too.
        raise AppError(
            ErrorCode.IDEMPOTENCY_KEY_REUSED,
            IDEMPOTENCY_KEY_REUSED_MESSAGE,
            details={},
        )
    return CommandOutcome(result=None, replayed=True, outcome=receipt.outcome_json)


def run_idempotent_command(
    session: Session,
    *,
    operation: Callable,
    operation_name: str,
    key: str | None,
    ctx: ExecutionContext,
    params: dict,
    **service_kwargs: object,
) -> CommandOutcome:
    """Execute ``operation`` exactly once per ``(org, operation, key)``.

    ``operation`` is the existing application service (``book_appointment``,
    ``cancel_appointment``, ``reschedule_appointment``): it owns its
    transaction (``session.begin()`` on an idle Session, A2) and receives the
    ``IdempotencyClaim`` through its optional ``idempotency`` parameter.

    ``params`` are the canonical domain parameters of the command (I4); the
    same values, plus ``ctx``, are forwarded to ``operation`` via
    ``service_kwargs``.

    * ``key is None`` → today's behaviour byte-for-byte, no receipt (I11);
    * agent/integration principal without a key → ``INVALID_INPUT`` (422)
      before any mutation (I10);
    * collision with the same fingerprint and principal → replay of the
      stored logical outcome (C1/C4);
    * any other collision → ``IDEMPOTENCY_KEY_REUSED`` (C2/C6).
    """
    if ctx.principal_type in KEY_REQUIRED_PRINCIPAL_TYPES and key is None:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            "An idempotency key is required for agent and integration principals.",
        )
    if (
        ctx.principal_type in KEY_REQUIRED_PRINCIPAL_TYPES
        and key is not None
        and not _is_canonical_uuid4(key)
    ):
        raise AppError(ErrorCode.INVALID_INPUT, IDEMPOTENCY_KEY_FORMAT_MESSAGE)
    if key is None:
        return CommandOutcome(result=operation(session, ctx=ctx, **service_kwargs))
    fingerprint = command_fingerprint(
        operation=operation_name, organization_id=ctx.organization_id, params=params
    )
    claim = IdempotencyClaim(operation=operation_name, key=key, fingerprint=fingerprint)
    try:
        result = operation(session, ctx=ctx, idempotency=claim, **service_kwargs)
    except IntegrityError as exc:
        if not _is_receipt_conflict(exc):
            raise
        return _replay_or_reject(session, ctx, operation_name, key, fingerprint)
    return CommandOutcome(result=result, replayed=False)
