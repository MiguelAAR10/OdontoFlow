"""B2 — generic agent proposals, inbox, approve/decline (collection kinds).

Spec: ``docs/superpowers/specs/2026-10-01-erp-b2.md``. Real PostgreSQL, one
pytest process (the concurrency tests use two sessions + ``Barrier``).
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker
from test_appointment_proposals import _seed_proposal, _seed_tenant
from test_domain_gaps_helpers import actor_ctx, make_charge, pay, seed_booking

from app import create_app
from app.audit.models import AuditEvent
from app.clinical.models import ServiceExecution, Visit
from app.context import default_context
from app.db import get_db
from app.economics.models import Charge, ChargeFollowUp
from app.economics.schemas import ChargeFollowUpCreate
from app.economics.service import open_follow_up
from app.errors import AppError
from app.iam.credentials import issue_credential
from app.iam.service import (
    add_membership,
    assign_role,
    create_principal,
    create_role,
    grant_permission,
    provision_system_access,
)
from app.messaging.models import ChannelAccount, ContactIdentity, Conversation, OutboundMessage
from app.organization.models import Organization
from app.proposals.models import AgentProposal
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID as ORG
from scripts.issue_credential import _assign_profile, _resolve_principal

REMINDER_TEXT = "Hola, le recordamos su saldo pendiente en la clínica."


# --- fixtures & helpers -------------------------------------------------------


@pytest.fixture
def maker(migrated_engine):
    return sessionmaker(bind=migrated_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def client(maker):
    app = create_app()

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.state.auth_sessionmaker = maker
    return TestClient(app, raise_server_exceptions=False)


def _credential(session, *, name, principal_type, profile, organization_id=ORG):
    principal = _resolve_principal(
        session, organization_id=organization_id, name=name, principal_type=principal_type
    )
    _assign_profile(
        session, organization_id=organization_id, principal_id=principal.id, profile=profile
    )
    _credential_row, token = issue_credential(
        session, organization_id=organization_id, principal_id=principal.id, name=name
    )
    session.commit()
    return principal.id, {"Authorization": f"Bearer {token}"}


def _agent(session, organization_id=ORG):
    return _credential(
        session,
        name="airy-cobranza-test",
        principal_type="agent",
        profile="collections-agent",
        organization_id=organization_id,
    )


def _integration(session):
    return _credential(
        session, name="n8n-cobranza", principal_type="integration", profile="collections-agent"
    )


def _lucia(session, organization_id=ORG):
    return _credential(
        session,
        name="Lucía Ramos",
        principal_type="human",
        profile="secretaria",
        organization_id=organization_id,
    )


def _carlos(session):
    return _credential(
        session, name="Carlos Vega", principal_type="human", profile="administrador"
    )


def _human_with(session, codes: tuple[str, ...], *, name="Humano Limitado"):
    principal = create_principal(session, display_name=name, principal_type="human")
    membership = add_membership(session, organization_id=ORG, principal_id=principal.id)
    role = create_role(session, organization_id=ORG, code=f"b2-{principal.id}", name="b2")
    for code in codes:
        grant_permission(session, role_id=role.id, permission_code=code)
    assign_role(session, organization_id=ORG, membership_id=membership.id, role_id=role.id)
    _row, token = issue_credential(
        session, organization_id=ORG, principal_id=principal.id, name=name
    )
    session.commit()
    return principal.id, {"Authorization": f"Bearer {token}"}


def _other_org(session, name="Clinica B2") -> int:
    org = Organization(name=name)
    session.add(org)
    session.commit()
    provision_system_access(session, org.id)
    session.commit()
    return org.id


_SUFFIX = iter(range(1, 10_000))


def _seed_charge(
    session,
    *,
    organization_id=ORG,
    conversation=True,
    consent="opted_in",
    ids=None,
):
    """Charge of S/ 150.00 for a new patient, optionally reachable on WhatsApp."""
    n = next(_SUFFIX)
    ids = ids or seed_booking(session, organization_id=organization_id, suffix=f"b2-{n}")
    charge_id = make_charge(session, ids, dni=f"72{n:06d}")
    patient_id = session.scalar(
        select(Visit.patient_id)
        .join(ServiceExecution, ServiceExecution.visit_id == Visit.id)
        .join(Charge, Charge.service_execution_id == ServiceExecution.id)
        .where(Charge.id == charge_id)
    )
    session.rollback()
    seeded = {
        "ids": ids,
        "charge_id": charge_id,
        "patient_id": patient_id,
        "location_id": ids["location_id"],
        "conversation_id": None,
        "contact_id": None,
    }
    if conversation:
        channel = ChannelAccount(
            organization_id=organization_id,
            provider="whatsapp",
            external_account_id=f"wa-b2-{n}",
            phone_number_id=f"phone-b2-{n}",
            display_name=f"WhatsApp b2 {n}",
            is_active=True,
        )
        session.add(channel)
        session.flush()
        contact = ContactIdentity(
            organization_id=organization_id,
            channel_account_id=channel.id,
            external_contact_id=f"contact-b2-{n}",
            normalized_phone_e164=f"+5198{n:07d}",
            patient_id=patient_id,
            consent_status=consent,
        )
        session.add(contact)
        session.flush()
        conv = Conversation(
            organization_id=organization_id,
            channel_account_id=channel.id,
            contact_identity_id=contact.id,
            status="open",
            last_message_at=datetime.now(UTC),
        )
        session.add(conv)
        session.commit()
        seeded["conversation_id"] = conv.id
        seeded["contact_id"] = contact.id
    return seeded


def _key():
    return {"Idempotency-Key": str(uuid4())}


def _reminder(charge_id):
    return {"charge_id": charge_id, "message_text": REMINDER_TEXT}


def _follow(charge_id):
    return {"charge_id": charge_id, "next_follow_up_on": (date.today() + timedelta(days=5)).isoformat()}


def _create(client, headers, kind, payload, *, key=None, reason="Saldo vencido hace 9 días", **extra):
    body = {"kind": kind, "payload": payload, "reason": reason, **extra}
    return client.post(
        "/agent/proposals",
        json=body,
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
    )


def _approve(client, headers, item, *, key=None, payload_hash=None):
    return client.post(
        f"/agent/proposals/{item['id']}/approve",
        json={"payload_hash": payload_hash or item["payload_hash"]},
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
    )


def _decline(client, headers, proposal_id, note=None):
    body = {"note": note} if note else {}
    return client.post(f"/agent/proposals/{proposal_id}/decline", json=body, headers=headers)


def _row(session, proposal_id) -> AgentProposal:
    session.expire_all()
    row = session.get(AgentProposal, proposal_id)
    session.rollback()
    return row


def _count(session, model, *where) -> int:
    session.expire_all()
    value = session.scalar(select(func.count()).select_from(model).where(*where))
    session.rollback()
    return value


def _backdate(session, proposal_id):
    """``expires_at > created_at`` is a CHECK: move both into the past."""
    session.execute(
        text(
            "UPDATE agent_proposals SET created_at = now() - interval '4 days', "
            "expires_at = now() - interval '1 day' WHERE id = :id"
        ),
        {"id": proposal_id},
    )
    session.commit()


# --- 1. create ----------------------------------------------------------------


def test_agent_creates_a_proposal_with_server_computed_fields_and_dedupes(client, session):
    seeded = _seed_charge(session)
    _agent_id, agent = _agent(session)
    key = str(uuid4())

    created = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"]),
                      key=key, evidence={"days_overdue": 9, "balance": "1.00"})
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["source"] == "agent_proposal"
    assert item["status"] == "pending"
    assert item["agent_key"] == "reception"  # resolve_agent_key default (B1)
    assert item["kind"] == "collection_reminder"
    assert item["payload"] == _reminder(seeded["charge_id"])
    assert len(item["payload_hash"]) == 64
    assert item["subject"] == {"type": "charge", "id": str(seeded["charge_id"])}
    assert item["location_id"] == seeded["location_id"]
    assert item["conversation_id"] == seeded["conversation_id"]
    # Displayed facts come from the charge, never from the agent's evidence.
    assert item["facts"]["balance"] == "150.00"
    assert "S/ 150.00" in item["summary"]
    assert item["evidence"] == {"days_overdue": 9, "balance": "1.00"}
    assert item["actions"] == []
    assert item["decided_by"] is None

    again = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"]))
    assert again.status_code == 200, again.text
    assert again.json()["id"] == item["id"]

    replay = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"]),
                     key=key, evidence={"days_overdue": 9, "balance": "1.00"})
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == item["id"]
    assert _count(session, AgentProposal) == 1


def test_integration_agent_key_is_its_display_name(client, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, integration = _integration(session)
    created = _create(client, integration, "collection_follow_up", _follow(seeded["charge_id"]))
    assert created.status_code == 201, created.text
    assert created.json()["agent_key"] == "n8n-cobranza"


def test_body_agent_key_and_unknown_fields_are_rejected(client, session):
    seeded = _seed_charge(session)
    _pid, agent = _agent(session)
    spoofed = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"]),
                      agent_key="airy-cobranza")
    assert spoofed.status_code == 422, spoofed.text
    extra = _create(client, agent, "collection_reminder",
                    {**_reminder(seeded["charge_id"]), "amount": "1.00"})
    assert extra.status_code == 422, extra.text
    missing_key = client.post(
        "/agent/proposals",
        json={"kind": "collection_reminder", "payload": _reminder(seeded["charge_id"]),
              "reason": "x"},
        headers=agent,
    )
    assert missing_key.status_code == 422, missing_key.text
    assert _count(session, AgentProposal) == 0


def test_a_human_cannot_create_a_proposal(client, session):
    seeded = _seed_charge(session)
    _pid, lucia = _lucia(session)
    response = _create(client, lucia, "collection_reminder", _reminder(seeded["charge_id"]))
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert _count(session, AgentProposal) == 0


def test_create_rejects_unreachable_paid_and_already_followed_charges(client, session):
    _pid, agent = _agent(session)
    no_conversation = _seed_charge(session, conversation=False)
    opted_out = _seed_charge(session, consent="opted_out")
    paid = _seed_charge(session)
    followed = _seed_charge(session, conversation=False)
    pay(session, paid["charge_id"], "150.00")
    open_follow_up(
        session,
        followed["charge_id"],
        ChargeFollowUpCreate(next_follow_up_on=date.today() + timedelta(days=3)),
        ctx=default_context(ORG),
    )

    for seeded in (no_conversation, opted_out):
        response = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"]))
        assert response.status_code == 404, response.text
    response = _create(client, agent, "collection_reminder", _reminder(paid["charge_id"]))
    assert response.status_code == 422, response.text
    response = _create(client, agent, "collection_follow_up", _follow(paid["charge_id"]))
    assert response.status_code == 422, response.text
    response = _create(client, agent, "collection_follow_up", _follow(followed["charge_id"]))
    assert response.status_code == 422, response.text
    assert _count(session, AgentProposal) == 0


def test_an_expired_open_proposal_never_blocks_a_new_one(client, session):
    seeded = _seed_charge(session)
    _pid, agent = _agent(session)
    first = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"])).json()
    _backdate(session, first["id"])

    read = client.get(f"/agent/proposals/{first['id']}", headers=agent)
    assert read.status_code == 200, read.text
    assert read.json()["status"] == "expired"
    assert _row(session, first["id"]).status == "pending"  # reads never write

    second = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"]))
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first["id"]
    assert _row(session, first["id"]).status == "expired"


def test_concurrent_creates_for_one_subject_yield_one_row(maker, session):
    from app.iam.permissions import PROPOSALS_CREATE
    from app.proposals.service import submit_proposal

    seeded = _seed_charge(session, conversation=False)
    agent_ctx = actor_ctx(session, codes=(PROPOSALS_CREATE,), principal_type="agent")
    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        own = maker()
        try:
            barrier.wait()
            results.append(
                submit_proposal(
                    own,
                    ctx=agent_ctx,
                    kind="collection_follow_up",
                    payload=_follow(seeded["charge_id"]),
                    reason="Saldo vencido",
                    key=str(uuid4()),
                )
            )
        except Exception as exc:  # pragma: no cover - surfaced by the assert
            results.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not isinstance(r, Exception) for r in results), results
    assert len({r.proposal_id for r in results}) == 1
    assert sorted(r.created for r in results) == [False, True]
    assert _count(session, AgentProposal) == 1


def test_an_agent_cannot_propose_on_another_tenants_charge(client, session):
    other = _other_org(session)
    foreign = _seed_charge(session, organization_id=other, conversation=False)
    _pid, agent = _agent(session)
    response = _create(client, agent, "collection_follow_up", _follow(foreign["charge_id"]))
    assert response.status_code == 404, response.text
    assert _count(session, AgentProposal) == 0


# --- 2-4. hash, expiry, drift -------------------------------------------------


def test_approve_with_a_wrong_hash_is_409_and_the_row_stays_pending(client, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, agent = _agent(session)
    _lid, lucia = _lucia(session)
    item = _create(client, agent, "collection_follow_up", _follow(seeded["charge_id"])).json()

    response = _approve(client, lucia, item, payload_hash="0" * 64)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROPOSAL_HASH_MISMATCH"
    assert _row(session, item["id"]).status == "pending"
    assert _count(session, ChargeFollowUp) == 0


def test_approve_after_expiry_is_410_and_marks_the_row_expired(client, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, agent = _agent(session)
    _lid, lucia = _lucia(session)
    item = _create(client, agent, "collection_follow_up", _follow(seeded["charge_id"])).json()
    _backdate(session, item["id"])

    response = _approve(client, lucia, item)
    assert response.status_code == 410, response.text
    assert response.json()["error"]["code"] == "PROPOSAL_EXPIRED"
    assert _row(session, item["id"]).status == "expired"
    again = _approve(client, lucia, item)
    assert again.status_code == 410, again.text
    assert _count(session, ChargeFollowUp) == 0


def test_a_payment_after_the_proposal_supersedes_it(client, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, agent = _agent(session)
    _lid, lucia = _lucia(session)
    item = _create(client, agent, "collection_follow_up", _follow(seeded["charge_id"])).json()
    pay(session, seeded["charge_id"], "50.00")

    response = _approve(client, lucia, item)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROPOSAL_SUPERSEDED"
    assert _row(session, item["id"]).status == "superseded"
    assert _count(session, ChargeFollowUp) == 0


def test_a_follow_up_opened_meanwhile_does_not_supersede_a_reminder(client, session):
    seeded = _seed_charge(session)
    _pid, agent = _agent(session)
    _lid, lucia = _lucia(session)
    item = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"])).json()
    open_follow_up(
        session,
        seeded["charge_id"],
        ChargeFollowUpCreate(next_follow_up_on=date.today() + timedelta(days=3)),
        ctx=default_context(ORG),
    )
    response = _approve(client, lucia, item)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed"


# --- 5. exactly once ------------------------------------------------------------


def test_double_approve_executes_once(client, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, agent = _agent(session)
    _lid, lucia = _lucia(session)
    item = _create(client, agent, "collection_follow_up", _follow(seeded["charge_id"])).json()
    key = str(uuid4())

    first = _approve(client, lucia, item, key=key)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "executed"
    assert first.json()["result_ref"]["type"] == "charge_follow_up"
    replay = _approve(client, lucia, item, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "executed"
    other = _approve(client, lucia, item)
    assert other.status_code == 409, other.text
    assert other.json()["error"]["code"] == "PROPOSAL_NOT_PENDING"
    assert other.json()["error"]["details"]["status"] == "executed"
    assert _count(session, ChargeFollowUp) == 1


def _human_ctx(session):
    from app.iam.permissions import DELIVERIES_CREATE, FOLLOW_UPS_CREATE, PROPOSALS_DECIDE

    return actor_ctx(session, codes=(PROPOSALS_DECIDE, FOLLOW_UPS_CREATE, DELIVERIES_CREATE))


def _race_approve(maker, ctx, proposal, keys):
    from app.proposals.service import approve_and_execute

    barrier = threading.Barrier(len(keys))
    results: list = []

    def worker(key):
        own = maker()
        try:
            barrier.wait()
            results.append(
                approve_and_execute(
                    own,
                    ctx=ctx,
                    proposal_id=proposal["id"],
                    payload_hash=proposal["payload_hash"],
                    key=key,
                ).status
            )
        except AppError as exc:
            results.append(exc.code.value)
        finally:
            own.close()

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_concurrent_approvals_with_different_keys_execute_once(client, maker, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, agent = _agent(session)
    item = _create(client, agent, "collection_follow_up", _follow(seeded["charge_id"])).json()
    results = _race_approve(maker, _human_ctx(session), item, [str(uuid4()), str(uuid4())])
    assert sorted(results) == ["PROPOSAL_NOT_PENDING", "executed"], results
    assert _count(session, ChargeFollowUp) == 1


def test_concurrent_same_key_approvals_send_one_message(client, maker, session):
    seeded = _seed_charge(session)
    _pid, agent = _agent(session)
    item = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"])).json()
    key = str(uuid4())
    results = _race_approve(maker, _human_ctx(session), item, [key, key])
    assert set(results) <= {"executed", "approved"}, results
    assert "executed" in results
    assert _count(session, OutboundMessage) == 1
    assert _row(session, item["id"]).status == "executed"


# --- 6. execution as the human -------------------------------------------------


def test_lucia_approves_a_reminder_and_the_audit_names_her(client, session):
    seeded = _seed_charge(session)
    _pid, agent = _agent(session)
    lucia_id, lucia = _lucia(session)
    item = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"])).json()
    listed = client.get("/agent/inbox", headers=lucia).json()["items"]
    assert [i["actions"] for i in listed if i["id"] == item["id"]] == [["approve", "decline"]]

    response = _approve(client, lucia, item)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "executed"
    assert body["decided_by"] == {"id": lucia_id, "display_name": "Lucía Ramos"}
    assert body["result_ref"]["type"] == "outbound_message"
    assert body["actions"] == []
    session.expire_all()
    outbound = session.scalars(select(OutboundMessage)).all()
    assert len(outbound) == 1
    assert outbound[0].id == body["result_ref"]["id"]
    assert outbound[0].conversation_id == seeded["conversation_id"]
    assert outbound[0].status == "pending"
    assert outbound[0].payload["text"] == REMINDER_TEXT
    audit = session.scalars(
        select(AuditEvent).where(
            AuditEvent.entity_type == "agent_proposal",
            AuditEvent.entity_id == str(item["id"]),
            AuditEvent.action.in_(("agent_proposal.approved", "agent_proposal.executed")),
        )
    ).all()
    session.rollback()
    assert {a.action for a in audit} == {"agent_proposal.approved", "agent_proposal.executed"}
    assert {(a.actor_id, a.actor_type) for a in audit} == {(str(lucia_id), "human")}


def test_a_closed_conversation_or_opt_out_fails_the_execution(client, session):
    _pid, agent = _agent(session)
    _lid, lucia = _lucia(session)
    closed = _seed_charge(session)
    opted = _seed_charge(session)
    closed_item = _create(client, agent, "collection_reminder", _reminder(closed["charge_id"])).json()
    opted_item = _create(client, agent, "collection_reminder", _reminder(opted["charge_id"])).json()
    session.execute(
        text("UPDATE conversations SET status = 'closed' WHERE id = :id"),
        {"id": closed["conversation_id"]},
    )
    session.execute(
        text("UPDATE contact_identities SET consent_status = 'opted_out' WHERE id = :id"),
        {"id": opted["contact_id"]},
    )
    session.commit()

    response = _approve(client, lucia, closed_item)
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["error_code"]) == ("failed", "ENTITY_INACTIVE")
    response = _approve(client, lucia, opted_item)
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["error_code"]) == ("failed", "NOT_FOUND")
    assert _count(session, OutboundMessage) == 0


# --- 7. who may decide -----------------------------------------------------------


def test_agents_and_integrations_cannot_approve_or_decline(client, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, agent = _agent(session)
    _iid, integration = _integration(session)
    item = _create(client, agent, "collection_follow_up", _follow(seeded["charge_id"])).json()
    for headers in (agent, integration):
        approve = _approve(client, headers, item)
        assert approve.status_code == 403, approve.text
        assert approve.json()["error"]["code"] == "PERMISSION_DENIED"
        decline = _decline(client, headers, item["id"])
        assert decline.status_code == 403, decline.text
    assert _row(session, item["id"]).status == "pending"


def test_a_human_without_the_kind_permission_cannot_approve(client, session):
    from app.iam.permissions import PROPOSALS_DECIDE, PROPOSALS_READ

    seeded = _seed_charge(session)
    _pid, agent = _agent(session)
    _hid, limited = _human_with(session, (PROPOSALS_READ, PROPOSALS_DECIDE))
    item = _create(client, agent, "collection_reminder", _reminder(seeded["charge_id"])).json()

    listed = client.get(f"/agent/proposals/{item['id']}", headers=limited).json()
    assert listed["actions"] == ["decline"]
    response = _approve(client, limited, item)
    assert response.status_code == 403, response.text
    assert _row(session, item["id"]).status == "pending"
    assert _count(session, OutboundMessage) == 0


# --- 8. decline --------------------------------------------------------------------


def test_decline_is_idempotent_and_blocks_approval(client, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, agent = _agent(session)
    lucia_id, lucia = _lucia(session)
    item = _create(client, agent, "collection_follow_up", _follow(seeded["charge_id"])).json()

    first = _decline(client, lucia, item["id"], note="Paciente ya coordinó")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "declined"
    assert first.json()["decided_by"]["id"] == lucia_id
    again = _decline(client, lucia, item["id"])
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "declined"
    assert _count(
        session, AuditEvent, AuditEvent.action == "agent_proposal.declined"
    ) == 1
    approve = _approve(client, lucia, item)
    assert approve.status_code == 409, approve.text
    assert approve.json()["error"]["details"]["status"] == "declined"


def test_decline_of_expired_or_executed_proposals(client, session):
    _pid, agent = _agent(session)
    _lid, lucia = _lucia(session)
    expired = _seed_charge(session, conversation=False)
    executed = _seed_charge(session, conversation=False)
    expired_item = _create(client, agent, "collection_follow_up", _follow(expired["charge_id"])).json()
    executed_item = _create(client, agent, "collection_follow_up", _follow(executed["charge_id"])).json()
    _backdate(session, expired_item["id"])
    assert _approve(client, lucia, executed_item).json()["status"] == "executed"

    response = _decline(client, lucia, expired_item["id"])
    assert response.status_code == 410, response.text
    assert _row(session, expired_item["id"]).status == "expired"
    response = _decline(client, lucia, executed_item["id"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_PENDING"


# --- 9. inbox ------------------------------------------------------------------------


def test_inbox_merges_both_sources_and_filters(client, session):
    _pid, agent = _agent(session)
    _cid, carlos = _carlos(session)
    tenant = _seed_tenant(session, organization_id=ORG, suffix="b2-inbox", phone="+51999220001")
    appointment = _seed_proposal(session, tenant, start_utc=datetime(2026, 12, 7, 14, tzinfo=UTC))
    reminder_charge = _seed_charge(session)
    follow_charge = _seed_charge(session, conversation=False)
    declined_charge = _seed_charge(session, conversation=False)
    expired_charge = _seed_charge(session, conversation=False)
    reminder = _create(client, agent, "collection_reminder", _reminder(reminder_charge["charge_id"])).json()
    follow = _create(client, agent, "collection_follow_up", _follow(follow_charge["charge_id"])).json()
    declined = _create(client, agent, "collection_follow_up", _follow(declined_charge["charge_id"])).json()
    expired = _create(client, agent, "collection_follow_up", _follow(expired_charge["charge_id"])).json()
    _decline(client, carlos, declined["id"])
    _backdate(session, expired["id"])

    def ids(**params):
        response = client.get("/agent/inbox", headers=carlos, params=params)
        assert response.status_code == 200, response.text
        return [(i["source"], i["id"]) for i in response.json()["items"]], response.json()

    pending, body = ids()
    assert set(pending) == {
        ("agent_proposal", reminder["id"]),
        ("agent_proposal", follow["id"]),
        ("appointment_proposal", appointment.id),
    }
    appt = next(i for i in body["items"] if i["source"] == "appointment_proposal")
    assert appt["kind"] == "appointment_booking"
    assert appt["conversation_id"] == tenant["conversation"].id
    assert appt["confirmation_token"] == str(appointment.confirmation_token)
    assert appt["actions"] == ["approve", "decline"]
    assert appt["status"] == "pending"

    assert ids(source="agent_proposal")[0] == [
        ("agent_proposal", follow["id"]),
        ("agent_proposal", reminder["id"]),
    ]
    assert ids(kind="collection_reminder")[0] == [("agent_proposal", reminder["id"])]
    assert ids(source="appointment_proposal")[0] == [("appointment_proposal", appointment.id)]
    assert set(ids(location_id=follow_charge["location_id"])[0]) == {
        ("agent_proposal", follow["id"])
    }
    assert ids(status="declined")[0] == [("agent_proposal", declined["id"])]
    assert ids(status="expired")[0] == [("agent_proposal", expired["id"])]
    bad = client.get("/agent/inbox", headers=carlos, params={"cursor": "not-a-cursor"})
    assert bad.status_code == 422, bad.text


def test_inbox_pagination_is_stable_while_rows_are_inserted(client, session):
    _pid, agent = _agent(session)
    _lid, lucia = _lucia(session)
    ids = seed_booking(session, suffix="b2-page")
    charges = [_seed_charge(session, conversation=False, ids=ids) for _ in range(9)]
    created = [
        _create(client, agent, "collection_follow_up", _follow(c["charge_id"])).json()["id"]
        for c in charges[:7]
    ]

    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = client.get("/agent/inbox", headers=lucia, params=params).json()
        seen.extend(i["id"] for i in body["items"])
        pages += 1
        if pages == 1:
            for c in charges[7:]:
                _create(client, agent, "collection_follow_up", _follow(c["charge_id"]))
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == sorted(created, reverse=True)


# --- 10. tenant isolation ----------------------------------------------------------


def test_another_tenant_cannot_see_or_decide(client, session):
    seeded = _seed_charge(session, conversation=False)
    _pid, agent = _agent(session)
    item = _create(client, agent, "collection_follow_up", _follow(seeded["charge_id"])).json()
    other = _other_org(session)
    _oid, foreign = _lucia(session, organization_id=other)

    assert client.get(f"/agent/proposals/{item['id']}", headers=foreign).status_code == 404
    assert _approve(client, foreign, item).status_code == 404
    assert _decline(client, foreign, item["id"]).status_code == 404
    inbox = client.get("/agent/inbox", headers=foreign, params={"status": "pending"})
    assert inbox.status_code == 200, inbox.text
    assert inbox.json() == {"items": [], "next_cursor": None}
    assert _row(session, item["id"]).status == "pending"


# --- 11. registry guards --------------------------------------------------------------


def test_no_kind_ever_requires_an_l4_permission():
    from app.iam.permissions import PAYMENTS_MANAGE, PAYMENTS_REVERSE, PERMISSION_CODES
    from app.proposals.executors import KINDS

    required = {spec.required_permission for spec in KINDS.values()}
    assert set(KINDS) == {"collection_reminder", "collection_follow_up"}
    assert required <= set(PERMISSION_CODES)
    assert not required & {PAYMENTS_REVERSE, PAYMENTS_MANAGE}
