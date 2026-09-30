"""B0.5 §2 — total payment reversal as a new record (spec 2026-10-01)."""

from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditEvent
from app.context import default_context
from app.economics.models import Payment, PaymentReversal
from app.economics.schemas import ChargeFollowUpCreate, PaymentReverse
from app.economics.service import charge_paid_amount, open_follow_up, reverse_payment
from app.errors import AppError, ErrorCode
from app.iam.permissions import PAYMENTS_READ, PAYMENTS_REVERSE
from app.iam.service import IamErrorCode
from app.idempotency.models import CommandReceipt
from app.idempotency.service import IdempotencyClaim
from app.organization.service import create_organization
from test_domain_gaps_helpers import (  # noqa: F401  (``api`` is a fixture)
    ORG,
    actor_ctx,
    api,
    domain_events,
    make_charge,
    pay,
    seed_booking,
)


def _count(session, model) -> int:
    value = session.scalar(select(func.count()).select_from(model))
    session.rollback()
    return value


def _cashier(session):
    """A human principal holding ``payments.reverse`` (reversal is human-only, L4)."""
    return actor_ctx(session, codes=(PAYMENTS_REVERSE,))


def _charge_status(api, charge_id: int) -> str:
    response = api.get(f"/charges/{charge_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    paid, outstanding = Decimal(body["paid"]), Decimal(body["outstanding"])
    if paid == 0:
        return "unpaid"
    return "paid" if outstanding == 0 else "partial"


def test_reversing_the_only_payment_returns_the_charge_to_unpaid(api, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")
    assert _charge_status(api, charge_id) == "paid"

    response = api.post(
        f"/payments/{payment_id}/reverse",
        json={"reason": "Pago registrado por error"},
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reversed"] is True
    assert body["reversed_at"] is not None
    assert body["amount"] == "150.00"  # the payment row itself is untouched
    assert _charge_status(api, charge_id) == "unpaid"
    assert charge_paid_amount(session, charge_id, ORG) == Decimal("0")
    session.rollback()
    unpaid = api.get("/charges", params={"status": "unpaid"}).json()
    assert charge_id in {row["id"] for row in unpaid}
    assert len(domain_events(session, "payment.reversed", str(payment_id))) == 1
    audit = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "payment.reversed", AuditEvent.entity_id == str(payment_id))
    )
    session.rollback()
    assert audit == 1


def test_reversing_one_of_two_payments_leaves_the_charge_partial(api, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    pay(session, charge_id, "100.00")
    second = pay(session, charge_id, "50.00")

    response = api.post(f"/payments/{second}/reverse", json={"reason": "Tarjeta rechazada"})

    assert response.status_code == 200, response.text
    assert _charge_status(api, charge_id) == "partial"
    partial = api.get("/charges", params={"status": "partial"}).json()
    assert charge_id in {row["id"] for row in partial}


def test_a_new_payment_is_accepted_up_to_the_restored_balance(api, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")
    reverse_payment(session, payment_id, PaymentReverse(reason="Duplicado"), ctx=_cashier(session))

    assert pay(session, charge_id, "150.00")
    with pytest.raises(AppError) as raised:
        pay(session, charge_id, "0.01")
    assert raised.value.code is ErrorCode.INVALID_INPUT


def test_a_payment_can_only_be_reversed_once(api, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")

    first = api.post(f"/payments/{payment_id}/reverse", json={"reason": "Error"})
    second = api.post(f"/payments/{payment_id}/reverse", json={"reason": "Otra vez"})

    assert first.status_code == 200, first.text
    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "INVALID_INPUT"
    assert _count(session, PaymentReversal) == 1


def test_same_idempotency_key_replays_the_reversal(api, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")
    key = str(uuid4())

    first = api.post(
        f"/payments/{payment_id}/reverse", json={"reason": "Error"}, headers={"Idempotency-Key": key}
    )
    replay = api.post(
        f"/payments/{payment_id}/reverse", json={"reason": "Error"}, headers={"Idempotency-Key": key}
    )

    assert first.status_code == replay.status_code == 200
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert _count(session, PaymentReversal) == 1
    assert len(domain_events(session, "payment.reversed")) == 1


def test_reason_is_required(api, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")

    missing = api.post(f"/payments/{payment_id}/reverse", json={})
    blank = api.post(f"/payments/{payment_id}/reverse", json={"reason": ""})

    assert missing.status_code == blank.status_code == 422
    assert _count(session, PaymentReversal) == 0


def test_blank_reason_is_rejected_by_the_database(session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO payment_reversals "
                "(organization_id, payment_id, reason, created_by_principal_id) "
                "VALUES (:org, :payment, '   ', 1)"
            ),
            {"org": ORG, "payment": payment_id},
        )
        session.flush()
    session.rollback()


@pytest.mark.parametrize("principal_type", ["agent", "integration"])
def test_only_a_human_principal_may_reverse_a_payment(session, principal_type):
    """Plan §2 principle 5: reversals are L4 — agent and integration are refused alike."""
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")
    caller = actor_ctx(session, codes=(PAYMENTS_REVERSE,), principal_type=principal_type)
    receipts_before = _count(session, CommandReceipt)

    with pytest.raises(AppError) as raised:
        reverse_payment(
            session,
            payment_id,
            PaymentReverse(reason="No humano"),
            ctx=caller,
            idempotency=IdempotencyClaim(
                operation="payments.reverse", key=str(uuid4()), fingerprint="b05-non-human"
            ),
        )

    assert raised.value.code is ErrorCode.INVALID_INPUT
    assert raised.value.message == "Only an authenticated human principal may reverse a payment."
    session.rollback()
    assert _count(session, PaymentReversal) == 0
    assert _count(session, CommandReceipt) == receipts_before
    assert domain_events(session, "payment.reversed") == []


def test_the_system_principal_cannot_reverse_a_payment(session):
    ids = seed_booking(session)
    payment_id = pay(session, make_charge(session, ids), "150.00")

    with pytest.raises(AppError) as raised:
        reverse_payment(session, payment_id, PaymentReverse(reason="Sistema"), ctx=default_context(ORG))

    assert raised.value.code is ErrorCode.INVALID_INPUT
    session.rollback()
    assert _count(session, PaymentReversal) == 0
    assert domain_events(session, "payment.reversed") == []


def test_reversal_requires_the_permission(session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "150.00")
    reader = actor_ctx(session, codes=(PAYMENTS_READ,))

    with pytest.raises(AppError) as raised:
        reverse_payment(session, payment_id, PaymentReverse(reason="Sin permiso"), ctx=reader)
    assert raised.value.code == IamErrorCode.PERMISSION_DENIED
    session.rollback()
    assert _count(session, PaymentReversal) == 0


def test_other_organization_payment_is_not_found(api, session):
    org_b = create_organization(session, "Clínica Pagos B").id
    session.commit()
    ids_b = seed_booking(session, organization_id=org_b, suffix="pb")
    charge_b = make_charge(session, ids_b, dni="71000077")
    payment_b = pay(session, charge_b, "150.00", organization_id=org_b)

    response = api.post(f"/payments/{payment_b}/reverse", json={"reason": "Ajeno"})

    assert response.status_code == 404, response.text
    assert _count(session, PaymentReversal) == 0


def test_cross_tenant_reversal_is_impossible_at_the_database(session):
    org_b = create_organization(session, "Clínica Pagos C").id
    session.commit()
    ids = seed_booking(session)
    payment_id = pay(session, make_charge(session, ids), "150.00")
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO payment_reversals "
                "(organization_id, payment_id, reason, created_by_principal_id) "
                "VALUES (:org_b, :payment, 'cruzado', 1)"
            ),
            {"org_b": org_b, "payment": payment_id},
        )
        session.flush()
    session.rollback()


def test_follow_up_outstanding_reflects_the_reversal(api, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    payment_id = pay(session, charge_id, "100.00")
    open_follow_up(
        session,
        charge_id,
        ChargeFollowUpCreate(next_follow_up_on=date.today() + timedelta(days=3)),
        ctx=default_context(ORG),
    )
    reverse_payment(session, payment_id, PaymentReverse(reason="Error"), ctx=_cashier(session))

    rows = api.get(f"/charges/{charge_id}/follow-ups").json()
    assert rows and rows[0]["charge_outstanding"] == "150.00"


def test_payments_listing_marks_reversed_rows(api, session):
    ids = seed_booking(session)
    charge_id = make_charge(session, ids)
    kept = pay(session, charge_id, "50.00")
    reversed_id = pay(session, charge_id, "50.00")
    reverse_payment(session, reversed_id, PaymentReverse(reason="Error"), ctx=_cashier(session))

    rows = {row["id"]: row for row in api.get(f"/charges/{charge_id}/payments").json()}
    assert rows[kept]["reversed"] is False
    assert rows[reversed_id]["reversed"] is True
    assert _count(session, Payment) == 2  # nothing deleted or edited


def test_concurrent_reversals_create_exactly_one_row(migrated_engine, session):
    ids = seed_booking(session)
    payment_id = pay(session, make_charge(session, ids), "150.00")
    cashier = _cashier(session)
    maker = sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(label: str) -> None:
        own = maker()
        try:
            barrier.wait()
            reverse_payment(own, payment_id, PaymentReverse(reason=label), ctx=cashier)
            outcomes.append("ok")
        except AppError as exc:
            outcomes.append(exc.code.value)
        finally:
            own.close()

    threads = [threading.Thread(target=worker, args=(f"r{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["INVALID_INPUT", "ok"]
    assert _count(session, PaymentReversal) == 1
