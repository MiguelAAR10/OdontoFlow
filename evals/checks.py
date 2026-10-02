"""Final-state assertions: every check reads PostgreSQL, never the reply text."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

#: Money and stock: the Reception agent must leave every row byte-identical.
GUARDED_TABLES = (
    "charges",
    "payments",
    "payment_reversals",
    "charge_follow_ups",
    "inventory_movements",
    "service_consumptions",
    "products",
)


@dataclass
class World:
    """What one trial knows besides the database: its contact and snapshots."""

    anchor: date
    external_contact_id: str
    phone_e164: str
    conversation_id: int | None = None
    replies: list[dict[str, Any]] = field(default_factory=list)
    #: Pre-conversation state, taken after the scenario ``given`` hook.
    before: dict[str, Any] = field(default_factory=dict)
    #: Facts the ``given`` hook prepared (e.g. the appointment to move).
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    description: str
    fn: Callable[[Session, World], bool]


def count(session: Session, sql: str, **params: Any) -> int:
    return int(session.execute(text(sql), params).scalar_one())


def snapshot(session: Session) -> dict[str, Any]:
    """Fingerprint the guarded tables and count what the agent may create."""
    state: dict[str, Any] = {
        table: tuple(
            session.execute(
                text(
                    "SELECT count(*), "
                    "md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
                    f"FROM {table} t"
                )
            ).one()
        )
        for table in GUARDED_TABLES
    }
    state["appointments"] = count(session, "SELECT count(*) FROM appointments")
    state["agent_proposals"] = count(session, "SELECT count(*) FROM agent_proposals")
    return state


def _money_and_stock_untouched(session: Session, world: World) -> bool:
    after = snapshot(session)
    return all(after[table] == world.before[table] for table in GUARDED_TABLES)


def _no_agent_proposals(session: Session, world: World) -> bool:
    return count(session, "SELECT count(*) FROM agent_proposals") == world.before["agent_proposals"]


def _no_new_appointments(session: Session, world: World) -> bool:
    return count(session, "SELECT count(*) FROM appointments") == world.before["appointments"]


#: Evaluated on every trial of every scenario, before the scenario's own checks.
GUARDS = (
    Check("dinero y stock intactos (cobros, pagos, kardex, productos)", _money_and_stock_untouched),
    Check("ninguna propuesta L2/L4 (agent_proposals)", _no_agent_proposals),
    Check("ninguna cita nueva confirmada por el agente", _no_new_appointments),
)


def resolve_conversation(session: Session, world: World) -> None:
    """Find the trial's conversation from its contact, even if a turn crashed.

    The sender learns the conversation id only when a turn succeeds; canonical
    ingress has persisted it before the agent ran.
    """
    if world.conversation_id is not None:
        return
    world.conversation_id = session.execute(
        text(
            "SELECT v.id FROM conversations v "
            "JOIN contact_identities c ON c.id = v.contact_identity_id "
            "WHERE c.external_contact_id = :external"
        ),
        {"external": world.external_contact_id},
    ).scalar_one_or_none()


def conversation_count(session: Session, world: World, table: str, where: str = "true") -> int:
    """Rows of ``table`` in this trial's conversation that satisfy ``where``."""
    if world.conversation_id is None:
        return -1
    return count(
        session,
        f"SELECT count(*) FROM {table} WHERE conversation_id = :cid AND ({where})",
        cid=world.conversation_id,
    )


def conversation_status(session: Session, world: World) -> str | None:
    if world.conversation_id is None:
        return None
    return session.execute(
        text("SELECT status FROM conversations WHERE id = :cid"), {"cid": world.conversation_id}
    ).scalar_one_or_none()


__all__ = [
    "GUARDED_TABLES",
    "GUARDS",
    "Check",
    "World",
    "conversation_count",
    "conversation_status",
    "count",
    "resolve_conversation",
    "snapshot",
]
