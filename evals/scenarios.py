"""Five AIRY Recepción scenarios in Peruvian Spanish, judged on final state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.messaging.models import ContactIdentity
from evals.checks import Check, World, conversation_count, conversation_status
from evals.dates import booking_day, spanish_day

#: Sandbox channel created by ``scripts/seed_demo.py`` (COB).
SANDBOX_CHANNEL = "sandbox-local"


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    #: Patient messages, sent in order; ``{dia}`` is the booking day in Spanish.
    messages: tuple[str, ...]
    checks: tuple[Check, ...]
    #: Optional per-trial preparation on the cloned database (committed by the harness).
    given: Callable[[Session, World], None] | None = None

    def render(self, world: World) -> list[str]:
        dia = spanish_day(booking_day(world.anchor))
        return [message.format(dia=dia) for message in self.messages]

    def with_checks(self, key: str, checks: Iterable[Check]) -> "Scenario":
        return replace(self, key=key, checks=tuple(checks))


# --- given hooks ----------------------------------------------------------


def link_contact_to_future_appointment(session: Session, world: World) -> None:
    """Bind the trial's sandbox contact to a seeded patient with a future booking."""
    row = session.execute(
        text(
            "SELECT a.id, a.start_utc, a.lead_id, a.patient_id, l.contact_phone "
            "FROM appointments a JOIN leads l ON l.id = a.lead_id "
            "WHERE a.state = 'confirmed' AND a.start_utc > now() + interval '1 day' "
            "AND NOT EXISTS (SELECT 1 FROM contact_identities c "
            "                WHERE c.normalized_phone_e164 = l.contact_phone) "
            "ORDER BY a.start_utc, a.id LIMIT 1"
        )
    ).one()
    channel = session.execute(
        text(
            "SELECT id, organization_id FROM channel_accounts WHERE provider = 'sandbox' "
            "AND external_account_id = :external"
        ),
        {"external": SANDBOX_CHANNEL},
    ).one()
    session.add(
        ContactIdentity(
            organization_id=channel.organization_id,
            channel_account_id=channel.id,
            external_contact_id=world.external_contact_id,
            normalized_phone_e164=row.contact_phone,
            lead_id=row.lead_id,
            patient_id=row.patient_id,
            consent_status="opted_in",
        )
    )
    session.flush()
    world.phone_e164 = row.contact_phone
    world.facts["appointment_id"] = row.id
    world.facts["appointment_start"] = row.start_utc


# --- checks ---------------------------------------------------------------


def _proposals(status: str, expected: int) -> Check:
    return Check(
        f"{expected} propuesta(s) de cita '{status}' en la conversación",
        lambda session, world: (
            conversation_count(session, world, "appointment_proposals", f"status = '{status}'")
            == expected
        ),
    )


def _no_proposals() -> Check:
    return Check(
        "ninguna propuesta de cita en la conversación",
        lambda session, world: conversation_count(session, world, "appointment_proposals") == 0,
    )


def _pending_handoffs(expected: int, reason_code: str | None = None) -> Check:
    where = "status = 'pending'"
    label = f"{expected} derivación(es) a recepción pendiente(s)"
    if reason_code is not None:
        where += f" AND reason_code = '{reason_code}'"
        label += f" con motivo '{reason_code}'"
    return Check(
        label,
        lambda session, world: (
            conversation_count(session, world, "reception_handoffs", where) == expected
        ),
    )


def _status(expected: str) -> Check:
    return Check(
        f"conversación en estado '{expected}'",
        lambda session, world: conversation_status(session, world) == expected,
    )


def _one_reply_per_message() -> Check:
    return Check(
        "una respuesta saliente por mensaje del paciente",
        lambda session, world: (
            conversation_count(session, world, "outbound_messages")
            == conversation_count(session, world, "messages", "direction = 'inbound'")
        ),
    )


def _booking_is_free_cleaning_in_lince(session: Session, world: World) -> bool:
    if world.conversation_id is None:
        return False
    rows = session.execute(
        text(
            "SELECT p.start_utc, p.end_utc, p.practitioner_id, s.name AS service, l.name AS location "
            "FROM appointment_proposals p "
            "JOIN services s ON s.id = p.service_id "
            "JOIN locations l ON l.id = p.location_id "
            "WHERE p.conversation_id = :cid AND p.status = 'pending'"
        ),
        {"cid": world.conversation_id},
    ).all()
    if len(rows) != 1:
        return False
    proposal = rows[0]
    overlapping = session.execute(
        text(
            "SELECT count(*) FROM appointments WHERE practitioner_id = :pid "
            "AND state = 'confirmed' AND start_utc < :end AND end_utc > :start"
        ),
        {"pid": proposal.practitioner_id, "start": proposal.start_utc, "end": proposal.end_utc},
    ).scalar_one()
    future = session.execute(
        text("SELECT :start > now()"), {"start": proposal.start_utc}
    ).scalar_one()
    return (
        proposal.service == "Limpieza dental"
        and "Lince" in proposal.location
        and bool(future)
        and overlapping == 0
    )


def _appointment_untouched(session: Session, world: World) -> bool:
    row = session.execute(
        text("SELECT state, start_utc FROM appointments WHERE id = :id"),
        {"id": world.facts["appointment_id"]},
    ).one()
    return row.state == "confirmed" and row.start_utc == world.facts["appointment_start"]


def _no_reschedule_or_cancellation_proposals() -> Check:
    return Check(
        "ninguna propuesta de reprogramación ni de cancelación",
        lambda session, world: (
            conversation_count(session, world, "appointment_reschedule_proposals")
            + conversation_count(session, world, "appointment_cancellation_proposals")
            == 0
        ),
    )


_APPOINTMENT_UNTOUCHED = Check(
    "la cita existente sigue confirmada y en el mismo horario", _appointment_untouched
)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="agendar_limpieza",
        title="Agendar una limpieza en un horario libre",
        messages=(
            "Hola, buenas tardes. Quisiera separar una cita para una limpieza dental en la "
            "sede de Lince, el {dia} en la mañana si se puede. Soy Rosa Quispe.",
        ),
        checks=(
            _proposals("pending", 1),
            Check(
                "la propuesta es Limpieza dental en Lince, futura y sin choque de horario",
                _booking_is_free_cleaning_in_lince,
            ),
            _pending_handoffs(0),
            _status("awaiting_confirmation"),
            _one_reply_per_message(),
        ),
    ),
    Scenario(
        key="consulta_precio_horario",
        title="Preguntar precio y horario sin reservar",
        messages=(
            "Hola, ¿cuánto cuesta la limpieza dental? ¿Y hasta qué hora atienden los sábados "
            "en la sede de Jesús María?",
        ),
        checks=(
            _no_proposals(),
            _pending_handoffs(0),
            _status("open"),
            _one_reply_per_message(),
        ),
    ),
    Scenario(
        key="reprogramar_cita",
        title="Pedir reprogramar una cita existente",
        messages=(
            "Buenas, tengo una cita reservada pero me salió un viaje de trabajo. ¿Me la "
            "podrían reprogramar para la próxima semana, por favor?",
        ),
        given=link_contact_to_future_appointment,
        checks=(
            _APPOINTMENT_UNTOUCHED,
            _no_proposals(),
            _no_reschedule_or_cancellation_proposals(),
            _pending_handoffs(1),
            _status("human_handoff"),
        ),
    ),
    Scenario(
        key="cancelar_cita",
        title="Pedir cancelar una cita existente",
        messages=(
            "Hola, quería cancelar mi cita porque al final no voy a poder ir. Disculpe las "
            "molestias.",
        ),
        given=link_contact_to_future_appointment,
        checks=(
            _APPOINTMENT_UNTOUCHED,
            _no_proposals(),
            _no_reschedule_or_cancellation_proposals(),
            _pending_handoffs(1),
            _status("human_handoff"),
        ),
    ),
    Scenario(
        key="pedir_humano",
        title="Pedir hablar con una persona",
        messages=("Hola, prefiero hablar con una persona de recepción, por favor.",),
        checks=(
            _pending_handoffs(1, "requested_by_contact"),
            _no_proposals(),
            _status("human_handoff"),
        ),
    ),
)


__all__ = ["SCENARIOS", "Scenario", "link_contact_to_future_appointment"]
