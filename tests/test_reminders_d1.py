"""SELF — D-1 reminders: ``POST /agent-runs {agent_key: "confirmaciones"}``.

Spec: ``docs/superpowers/specs/2026-10-01-erp-self.md``. One SQL selection,
one fixed template, one ``outbound_messages`` row per appointment and start,
queued as the human caller. Real PostgreSQL, one pytest process.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from test_agent_proposals import _count, _credential, _human_with, _lucia, _other_org
from test_collections_sweep import _airy, client  # noqa: F401  (fixture)
from test_public_booking import _bff, _body, _book, _lead, _seed

from app.agents_runtime.models import AgentRun
from app.audit.models import AuditEvent
from app.clinical.schemas import PatientCreate
from app.clinical.service import create_patient
from app.context import default_context
from app.iam.permissions import APPOINTMENTS_READ, CHARGES_READ, DELIVERIES_CREATE
from app.iam.service import add_membership, assign_role, create_principal, create_role, grant_permission
from app.iam.credentials import issue_credential
from app.messaging.models import ChannelAccount, ContactIdentity, Conversation, OutboundMessage
from app.scheduling.models import Appointment
from app.scheduling.service import reschedule_appointment
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG

LIMA = ZoneInfo("America/Lima")
UTC = timezone.utc


@pytest.fixture(autouse=True)
def _enabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_CONFIRMACIONES_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_COBRANZA_ENABLED", raising=False)


# --- helpers ------------------------------------------------------------------


def _local_day(offset: int) -> date:
    return (datetime.now(LIMA) + timedelta(days=offset)).date()


def _at(offset: int, hour: int, minute: int = 0) -> datetime:
    d = _local_day(offset)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=LIMA)


_N = iter(range(1, 100_000))


def _appt(session, ids, start: datetime, *, lead_id, patient_id=None, state="confirmed") -> int:
    start_utc = start.astimezone(UTC)
    row = Appointment(
        organization_id=ids["organization_id"],
        lead_id=lead_id,
        patient_id=patient_id,
        service_id=ids["service_id"],
        practitioner_id=ids["practitioner_id"],
        location_id=ids["location_id"],
        start_utc=start_utc,
        end_utc=start_utc + timedelta(minutes=30),
        state=state,
    )
    session.add(row)
    session.commit()
    return row.id


def _conversation(session, organization_id, *, phone, patient_id=None, lead_id=None,
                  consent="opted_in", status="open") -> int:
    n = next(_N)
    channel = ChannelAccount(
        organization_id=organization_id,
        provider="whatsapp",
        external_account_id=f"wa-d1-{n}",
        phone_number_id=f"phone-d1-{n}",
        display_name=f"WA d1 {n}",
        is_active=True,
    )
    session.add(channel)
    session.flush()
    contact = ContactIdentity(
        organization_id=organization_id,
        channel_account_id=channel.id,
        external_contact_id=f"contact-d1-{n}",
        normalized_phone_e164=phone,
        patient_id=patient_id,
        lead_id=lead_id,
        consent_status=consent,
    )
    session.add(contact)
    session.flush()
    conv = Conversation(
        organization_id=organization_id,
        channel_account_id=channel.id,
        contact_identity_id=contact.id,
        status=status,
        last_message_at=datetime.now(UTC),
    )
    session.add(conv)
    session.commit()
    return conv.id


def _patient(session, organization_id, name) -> int:
    patient = create_patient(
        session, PatientCreate(full_name=name, dni=f"7{next(_N):07d}"),
        ctx=default_context(organization_id),
    )
    return patient.id


def _run(client, headers, *, key=None):
    return client.post(
        "/agent-runs", json={"agent_key": "confirmaciones"},
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
    )


def _counts(response) -> dict:
    assert response.status_code == 201, response.text
    return response.json()["counts"]


def _outbound(session, organization_id=ORG):
    session.expire_all()
    rows = session.scalars(
        select(OutboundMessage)
        .where(OutboundMessage.organization_id == organization_id)
        .order_by(OutboundMessage.id)
    ).all()
    session.rollback()
    return rows


def _machine(session, principal_type, codes):
    principal = create_principal(session, display_name=f"d1-{principal_type}", principal_type=principal_type)
    membership = add_membership(session, organization_id=ORG, principal_id=principal.id)
    role = create_role(session, organization_id=ORG, code=f"d1-{principal.id}", name="d1")
    for code in codes:
        grant_permission(session, role_id=role.id, permission_code=code)
    assign_role(session, organization_id=ORG, membership_id=membership.id, role_id=role.id)
    _row, token = issue_credential(
        session, organization_id=ORG, principal_id=principal.id, name=f"d1-{principal.id}"
    )
    session.commit()
    return {"Authorization": f"Bearer {token}"}


def _template(first, service, start: datetime, location) -> str:
    local = start.astimezone(LIMA)
    return (
        f"Hola {first}, le recordamos su cita de {service} mañana {local:%d/%m/%Y} a las "
        f"{local:%H:%M} en {location}. Si no puede asistir, responda este mensaje para "
        "reprogramar. ¡Gracias!"
    )


def _names(session, ids):
    from app.catalog.models import Service
    from app.organization.models import Location

    service = session.get(Service, ids["service_id"]).name
    location = session.get(Location, ids["location_id"]).name
    session.rollback()
    return service, location


# --- 8. selection and send --------------------------------------------------------


def test_run_reminds_each_reachable_appointment_of_tomorrow_once(client, session):
    ids = _seed(session)
    lucia_id, lucia = _lucia(session)
    service, location = _names(session, ids)

    lead_a = _lead(session, phone="+51911000001", name="Lead A")
    patient_a = _patient(session, ORG, "María Quispe")
    _conversation(session, ORG, phone="+51911000099", patient_id=patient_a)
    appt_a = _appt(session, ids, _at(1, 10), lead_id=lead_a, patient_id=patient_a)

    # Self-booked through the BFF, reachable only by phone match.
    _bff_id, bff = _bff(session)
    booked = _book(
        client, bff,
        _body(ids, start=_at(1, 11), phone="+51911000002", name="Jorge Salas"),
    )
    assert booked.status_code == 201, booked.text
    _conversation(session, ORG, phone="+51911000002")

    lead_c = _lead(session, phone="+51911000003", name="Sin Chat")
    _appt(session, ids, _at(1, 12), lead_id=lead_c)
    lead_d = _lead(session, phone="+51911000004", name="Cancelada")
    _conversation(session, ORG, phone="+51911000004")
    _appt(session, ids, _at(1, 13), lead_id=lead_d, state="cancelled")
    lead_e = _lead(session, phone="+51911000005", name="Hoy")
    _conversation(session, ORG, phone="+51911000005")
    _appt(session, ids, _at(0, 0, 15), lead_id=lead_e)
    lead_f = _lead(session, phone="+51911000006", name="Pasado")
    _conversation(session, ORG, phone="+51911000006")
    _appt(session, ids, _at(2, 10), lead_id=lead_f)

    counts = _counts(_run(client, lucia))
    assert counts == {"candidates": 3, "proposed": 2, "deduped": 0, "skipped": 1}
    rows = _outbound(session)
    assert len(rows) == 2
    texts = {row.payload["text"] for row in rows}
    assert texts == {
        _template("María", service, _at(1, 10), location),
        _template("Jorge", service, _at(1, 11), location),
    }
    audit = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "outbound.queued")
    ).all()
    assert {(a.actor_id, a.actor_type) for a in audit} == {(str(lucia_id), "human")}
    session.rollback()
    assert appt_a  # selected via patient_id


# --- 9. dedupe, replay, reachability, timezone, failure, reschedule ---------------


def test_rerun_dedupes_and_same_key_replays(client, session):
    ids = _seed(session)
    _lucia_id, lucia = _lucia(session)
    for n in range(2):
        lead = _lead(session, phone=f"+5191200000{n}", name=f"Lead {n}")
        _conversation(session, ORG, phone=f"+5191200000{n}")
        _appt(session, ids, _at(1, 9 + n), lead_id=lead)

    key = str(uuid4())
    first = _run(client, lucia, key=key)
    assert _counts(first) == {"candidates": 2, "proposed": 2, "deduped": 0, "skipped": 0}
    second = _run(client, lucia)
    assert _counts(second) == {"candidates": 2, "proposed": 0, "deduped": 2, "skipped": 0}
    assert len(_outbound(session)) == 2
    replay = _run(client, lucia, key=key)
    assert replay.status_code == 201 and replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert _count(session, AgentRun) == 2


def test_unreachable_and_timezone_edges(client, session):
    ids = _seed(session)
    _lucia_id, lucia = _lucia(session)
    opted = _lead(session, phone="+51913000001", name="Opt Out")
    _conversation(session, ORG, phone="+51913000001", consent="opted_out")
    _appt(session, ids, _at(1, 9), lead_id=opted)
    closed = _lead(session, phone="+51913000002", name="Cerrada")
    _conversation(session, ORG, phone="+51913000002", status="closed")
    _appt(session, ids, _at(1, 10), lead_id=closed)
    late = _lead(session, phone="+51913000003", name="Tarde Noche")
    _conversation(session, ORG, phone="+51913000003")
    _appt(session, ids, _at(1, 23, 30), lead_id=late)
    early = _lead(session, phone="+51913000004", name="Madrugada")
    _conversation(session, ORG, phone="+51913000004")
    _appt(session, ids, _at(2, 0, 30), lead_id=early)

    counts = _counts(_run(client, lucia))
    assert counts == {"candidates": 3, "proposed": 1, "deduped": 0, "skipped": 2}
    (row,) = _outbound(session)
    assert "a las 23:30" in row.payload["text"]


def test_a_failed_run_is_not_authoritative_and_the_rerun_never_duplicates(
    client, session, monkeypatch
):
    from app.agents_runtime import confirmaciones

    ids = _seed(session)
    _lucia_id, lucia = _lucia(session)
    for n in range(2):
        lead = _lead(session, phone=f"+5191400000{n}", name=f"Lead {n}")
        _conversation(session, ORG, phone=f"+5191400000{n}")
        _appt(session, ids, _at(1, 9 + n), lead_id=lead)

    real = confirmaciones.enqueue_outbound_message
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("provider exploded")
        return real(*args, **kwargs)

    monkeypatch.setattr(confirmaciones, "enqueue_outbound_message", flaky)
    failed = _run(client, lucia)
    assert failed.status_code == 500
    session.expire_all()
    run = session.scalars(select(AgentRun)).one()
    assert run.status == "failed" and run.error_category == "unexpected"
    session.rollback()
    assert len(_outbound(session)) == 1

    monkeypatch.setattr(confirmaciones, "enqueue_outbound_message", real)
    counts = _counts(_run(client, lucia))
    assert counts == {"candidates": 2, "proposed": 1, "deduped": 1, "skipped": 0}
    assert len(_outbound(session)) == 2


def test_a_rescheduled_appointment_gets_exactly_one_new_reminder(client, session):
    ids = _seed(session)
    _lucia_id, lucia = _lucia(session)
    lead = _lead(session, phone="+51915000001", name="Rosa Mori")
    _conversation(session, ORG, phone="+51915000001")
    appt = _appt(session, ids, _at(1, 10), lead_id=lead)
    assert _counts(_run(client, lucia))["proposed"] == 1

    reschedule_appointment(session, appt, _at(1, 15), ctx=default_context(ORG))
    assert _counts(_run(client, lucia)) == {
        "candidates": 1, "proposed": 1, "deduped": 0, "skipped": 0,
    }
    assert _counts(_run(client, lucia)) == {
        "candidates": 1, "proposed": 0, "deduped": 1, "skipped": 0,
    }
    rows = _outbound(session)
    assert len(rows) == 2 and "a las 15:00" in rows[1].payload["text"]


# --- 10. gate, kill switch, cobranza, tenant ---------------------------------------


def test_gate_kill_switch_and_tenant(client, session, monkeypatch):
    ids = _seed(session)
    lead = _lead(session, phone="+51916000001", name="Ana Org A")
    _conversation(session, ORG, phone="+51916000001")
    _appt(session, ids, _at(1, 10), lead_id=lead)

    _id, no_delivery = _human_with(session, (APPOINTMENTS_READ, CHARGES_READ), name="Sin Envio")
    response = _run(client, no_delivery)
    assert response.status_code == 403, response.text
    for principal_type in ("agent", "integration"):
        machine = _machine(session, principal_type, (APPOINTMENTS_READ, DELIVERIES_CREATE))
        response = _run(client, machine)
        assert response.status_code == 403, (principal_type, response.text)
    assert _count(session, AgentRun) == 0

    _lucia_id, lucia = _lucia(session)
    monkeypatch.setenv("AGENT_CONFIRMACIONES_ENABLED", "false")
    disabled = _run(client, lucia)
    assert disabled.status_code == 409, disabled.text
    assert disabled.json()["error"]["code"] == "AGENT_DISABLED"
    assert disabled.json()["error"]["details"]["reason"] == "disabled"
    assert _count(session, AgentRun) == 0

    _airy(session)
    cobranza = client.post(
        "/agent-runs", json={"agent_key": "cobranza"},
        headers={**lucia, "Idempotency-Key": str(uuid4())},
    )
    assert cobranza.status_code == 201, cobranza.text
    monkeypatch.delenv("AGENT_CONFIRMACIONES_ENABLED")

    org_b = _other_org(session, name="Clinica D1 B")
    _lucia_b, lucia_b = _lucia(session, organization_id=org_b)
    assert _counts(_run(client, lucia_b)) == {
        "candidates": 0, "proposed": 0, "deduped": 0, "skipped": 0,
    }
    assert _outbound(session) == []
    listed = client.get("/agent-runs", params={"agent_key": "confirmaciones"}, headers=lucia_b)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert _counts(_run(client, lucia))["proposed"] == 1
