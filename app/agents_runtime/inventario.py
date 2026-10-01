"""The inventory sweep (INV): SQL reads the ledger, code picks the donor.

One org-scoped read returns every reorder point (active product and location)
with its derived balance, 7-day consumption, latest movement id and whether
the subject is already handled. Python filters the short ones, allocates donor
surplus across them, and each becomes at most one ``inventory_transfer`` (a
donor sede has surplus above its own minimum) or ``inventory_entry`` (no donor)
proposal through B2's ``create_proposal``. The sweep never executes anything:
an administrador approves in B2.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Date, case, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from app.agents_runtime.cobranza import Counts
from app.economics.models import Product
from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.inventory.models import (
    ADJUSTMENT,
    ENTRADA,
    SALIDA,
    TRANSFER_IN,
    InventoryMovement,
    ReorderPoint,
)
from app.organization.models import Location
from app.proposals.executors import (
    PRODUCT_LOCATION_SUBJECT,
    ledger_version,
    normalize_payload,
    product_location_subject_id,
)
from app.proposals.models import AgentProposal
from app.proposals.service import create_proposal

AGENT_KEY = "inventario"
TRANSFER = "inventory_transfer"
ENTRY = "inventory_entry"
#: Coordinator decision: refill a short sede to twice its minimum
#: (``target_fill = TARGET_MULTIPLIER × min − balance``).
TARGET_MULTIPLIER = 2
CONSUMPTION_WINDOW_DAYS = 7
MAX_CANDIDATES = 200
SKIPPABLE = (ErrorCode.INVALID_INPUT, ErrorCode.NOT_FOUND, ErrorCode.ENTITY_INACTIVE)
CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Point:
    product_id: int
    product_name: str
    unit: str
    location_id: int
    location_name: str
    balance: Decimal
    min_quantity: Decimal
    consumption_7d: Decimal
    max_movement_id: int | None
    handled: bool


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def read_points(session: Session, organization_id: int) -> list[Point]:
    """The one selection query: every active reorder point of the org, no LIMIT."""
    same_point = (
        (InventoryMovement.organization_id == ReorderPoint.organization_id)
        & (InventoryMovement.product_id == ReorderPoint.product_id)
        & (InventoryMovement.location_id == ReorderPoint.location_id)
    )
    signed = case(
        (InventoryMovement.type.in_((ENTRADA, TRANSFER_IN, ADJUSTMENT)), InventoryMovement.quantity),
        else_=-InventoryMovement.quantity,
    )
    balance = (
        select(func.coalesce(func.sum(signed), 0)).where(same_point)
        .correlate(ReorderPoint).scalar_subquery()
    )
    consumption = (
        select(func.coalesce(func.sum(InventoryMovement.quantity), 0))
        .where(
            same_point,
            InventoryMovement.type == SALIDA,
            InventoryMovement.moved_at
            >= func.now() - func.make_interval(0, 0, 0, CONSUMPTION_WINDOW_DAYS),
        )
        .correlate(ReorderPoint).scalar_subquery()
    )
    last_movement = (
        select(func.max(InventoryMovement.id)).where(same_point)
        .correlate(ReorderPoint).scalar_subquery()
    )
    today = cast(func.timezone(Location.timezone, func.now()), Date)
    handled = exists().where(
        AgentProposal.organization_id == ReorderPoint.organization_id,
        AgentProposal.kind.in_((TRANSFER, ENTRY)),
        AgentProposal.subject_type == PRODUCT_LOCATION_SUBJECT,
        AgentProposal.subject_id
        == product_location_subject_id(ReorderPoint.product_id, ReorderPoint.location_id),
        or_(
            cast(func.timezone(Location.timezone, AgentProposal.created_at), Date) == today,
            AgentProposal.status == "approved",
            (AgentProposal.status == "pending") & (AgentProposal.expires_at > func.now()),
        ),
    )
    rows = session.execute(
        select(
            ReorderPoint.product_id,
            Product.name,
            Product.unit,
            ReorderPoint.location_id,
            Location.name,
            balance,
            ReorderPoint.min_quantity,
            consumption,
            last_movement,
            handled,
        )
        .join(
            Product,
            (Product.organization_id == ReorderPoint.organization_id)
            & (Product.id == ReorderPoint.product_id),
        )
        .join(
            Location,
            (Location.organization_id == ReorderPoint.organization_id)
            & (Location.id == ReorderPoint.location_id),
        )
        .where(
            ReorderPoint.organization_id == organization_id,
            Product.is_active.is_(True),
            Location.is_active.is_(True),
        )
    ).all()
    return [
        Point(
            product_id=row[0],
            product_name=row[1],
            unit=row[2],
            location_id=row[3],
            location_name=row[4],
            balance=Decimal(row[5]),
            min_quantity=Decimal(row[6]),
            consumption_7d=Decimal(row[7]),
            max_movement_id=row[8],
            handled=bool(row[9]),
        )
        for row in rows
    ]


def _target_json(point: Point) -> dict:
    return {
        "location_id": point.location_id,
        "name": point.location_name,
        "balance": _money(point.balance),
        "min_quantity": _money(point.min_quantity),
        "consumption_7d": _money(point.consumption_7d),
    }


def _pick_donor(target: Point, points: list[Point], surplus: dict) -> Point | None:
    donors = [
        p for p in points
        if p.product_id == target.product_id
        and p.location_id != target.location_id
        and surplus.get((p.product_id, p.location_id), 0) > 0
    ]
    if not donors:
        return None
    return min(donors, key=lambda p: (-surplus[(p.product_id, p.location_id)], p.location_id))


def _draft(run_id: int, target: Point, donor: Point | None, available: Decimal | None):
    """``(kind, raw_payload, reason, evidence, quantity)``; pure (no Session)."""
    fill = (TARGET_MULTIPLIER * target.min_quantity - target.balance).quantize(CENT)
    head = (
        f"{target.product_name}: {target.location_name} {_money(target.balance)} "
        f"< mín. {_money(target.min_quantity)}"
    )
    evidence = {
        "run_id": run_id,
        "product_id": target.product_id,
        "product_name": target.product_name,
        "unit": target.unit,
        "target": _target_json(target),
        "donor": None,
        "target_fill": _money(fill),
    }
    if donor is None:
        quantity = fill
        evidence["subject_version"] = ledger_version(target.max_movement_id)
        payload = {"product_id": target.product_id, "location_id": target.location_id,
                   "quantity": _money(quantity)}
        reason = f"{head}; sin sede donante, reponer {_money(quantity)}"
        kind = ENTRY
    else:
        quantity = min(fill, available).quantize(CENT)
        evidence["subject_version"] = ledger_version(donor.max_movement_id, target.max_movement_id)
        evidence["donor"] = {**_target_json(donor), "surplus": _money(available)}
        payload = {
            "product_id": target.product_id,
            "origin_location_id": donor.location_id,
            "destination_location_id": target.location_id,
            "quantity": _money(quantity),
        }
        reason = (
            f"{head}; traspasar {_money(quantity)} desde {donor.location_name} "
            f"({_money(donor.balance)}, mín. {_money(donor.min_quantity)})"
        )
        kind = TRANSFER
    evidence["quantity"] = _money(quantity)
    return kind, payload, reason, evidence, quantity


def sweep(session: Session, *, run_id: int, proposer: ExecutionContext) -> Counts:
    """Read, allocate and propose; ``proposer`` is only ever passed to create_proposal."""
    with session.begin():
        points = read_points(session, proposer.organization_id)
    surplus = {
        (p.product_id, p.location_id): p.balance - p.min_quantity
        for p in points
        if p.balance > p.min_quantity
    }
    candidates = sorted(
        (p for p in points if p.balance < p.min_quantity),
        key=lambda p: (p.location_id, p.product_id),
    )[:MAX_CANDIDATES]
    counts = Counts(candidates=len(candidates))
    for target in candidates:
        if target.handled:
            counts.deduped += 1
            continue
        donor = _pick_donor(target, points, surplus)
        available = surplus[(donor.product_id, donor.location_id)] if donor else None
        kind, raw, reason, evidence, quantity = _draft(run_id, target, donor, available)
        args, payload = normalize_payload(kind, raw)
        try:
            _proposal, created = create_proposal(
                session,
                ctx=proposer,
                kind=kind,
                args=args,
                payload=payload,
                reason=reason,
                evidence=evidence,
                agent_key=AGENT_KEY,
            )
        except AppError as exc:
            if exc.code not in SKIPPABLE:
                raise
            counts.skipped += 1
            continue
        if not created:
            counts.deduped += 1
            continue
        counts.proposed += 1
        if donor is not None:
            surplus[(donor.product_id, donor.location_id)] -= quantity
    return counts
