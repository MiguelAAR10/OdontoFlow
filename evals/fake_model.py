"""A deterministic, offline receptionist policy for the fake eval mode.

It never sees the scenario or its checks: it reads the latest patient message,
picks typed V0 tools the way a careful receptionist would, and answers with the
structured ``SalesAgentResponse``. The fake run therefore measures the harness,
the tool boundary and the backend contracts — not model quality.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from evals.dates import booking_day

FAKE_MODEL_NAME = "fake:reception-policy-v1"
LIMA = ZoneInfo("America/Lima")

_HUMAN = ("persona", "humano", "recepcionista", "asesor")
_RESCHEDULE = ("reprogram", "cambiar mi cita", "mover mi cita")
_CANCEL = ("cancelar", "anular")
_BOOKING = ("cita", "separar", "reservar", "agendar")
_NAME = re.compile(r"\b(?:soy|me llamo)\s+([a-záéíóúñ]+(?:\s+[a-záéíóúñ]+)?)", re.IGNORECASE)


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.lower())
    return "".join(char for char in folded if not unicodedata.combining(char))


def _intent(message: str) -> str:
    normalized = _normalize(message)
    for intent, words in (
        ("human", _HUMAN),
        ("reschedule", _RESCHEDULE),
        ("cancel", _CANCEL),
        ("booking", _BOOKING),
    ):
        if any(word in normalized for word in words):
            return intent
    return "information"


def _pick(rows: list[dict[str, Any]], message: str, *, fallback: str | None = None) -> dict | None:
    """The row whose (normalized) name — or its last words — the patient wrote."""
    normalized = _normalize(message)
    by_length = sorted(rows, key=lambda item: -len(item["name"]))
    for row in by_length:
        if _normalize(row["name"]) in normalized:
            return row
    # "ODONTO SMART Jesús María" is written "Jesús María".
    for row in by_length:
        words = _normalize(row["name"]).split(" ", 2)
        if len(words) == 3 and words[-1] in normalized:
            return row
    if fallback is not None:
        return next((row for row in rows if row["name"] == fallback), None)
    return rows[0] if rows else None


def build_fake_model(anchor: date):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class ReceptionFakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

        @staticmethod
        def _decode(message) -> dict[str, Any]:
            content = message.content
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    return {}
            return content if isinstance(content, dict) else {}

        @staticmethod
        def _call(name: str, args: dict[str, Any], sequence: int) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": f"eval-fake-{sequence}"}],
            )

        @classmethod
        def _answer(cls, reply: str, outcome: str, sequence: int) -> AIMessage:
            return cls._call(
                "SalesAgentResponse",
                {"reply": reply, "outcome": outcome, "handoff": outcome == "handoff"},
                sequence,
            )

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            last_human = max(index for index, item in enumerate(messages) if item.type == "human")
            text_value = str(messages[last_human].content)
            current = messages[last_human + 1 :]
            results = {
                getattr(item, "name", None): self._decode(item)
                for item in current
                if item.type == "tool"
            }
            sequence = sum(1 for item in messages if item.type == "ai") + 1
            message = self._step(_intent(text_value), text_value, results, sequence)
            return ChatResult(generations=[ChatGeneration(message=message)])

        def _step(self, intent, text_value, results, sequence) -> AIMessage:
            handoff = results.get("request_human_handoff")
            if handoff is not None:
                if handoff.get("status") == "success":
                    return self._answer(
                        "Te comunico con el equipo de recepción; en breve una persona te "
                        "atiende por este mismo chat.",
                        "handoff",
                        sequence,
                    )
                return self._answer(
                    "No pude derivarte en este momento; por favor llama a la sede.",
                    "continue",
                    sequence,
                )

            if intent == "human":
                return self._handoff(
                    "requested_by_contact", "El contacto pide hablar con una persona.", sequence
                )
            if intent in {"reschedule", "cancel"}:
                if "get_reception_context" not in results:
                    return self._call("get_reception_context", {}, sequence)
                action = "reprogramar" if intent == "reschedule" else "cancelar"
                return self._handoff(
                    "requested_by_contact",
                    f"El contacto pide {action} una cita existente; requiere recepción.",
                    sequence,
                )
            if intent == "booking":
                return self._booking(text_value, results, sequence)
            return self._information(text_value, results, sequence)

        def _handoff(self, reason_code: str, summary: str, sequence: int) -> AIMessage:
            return self._call(
                "request_human_handoff",
                {"reason_code": reason_code, "reason_summary": summary},
                sequence,
            )

        def _booking(self, text_value, results, sequence) -> AIMessage:
            if "list_services" not in results:
                return self._call("list_services", {}, sequence)
            if "list_locations" not in results:
                return self._call("list_locations", {}, sequence)
            services = (results["list_services"].get("data") or {}).get("services", [])
            locations = (results["list_locations"].get("data") or {}).get("locations", [])
            service = _pick(services, text_value, fallback="Limpieza dental")
            location = _pick(locations, text_value)
            if service is None or location is None:
                return self._handoff("low_confidence", "No identifiqué servicio o sede.", sequence)
            if "query_available_slots" not in results:
                day = booking_day(anchor)
                return self._call(
                    "query_available_slots",
                    {
                        "service_id": service["id"],
                        "location_id": location["id"],
                        "window_start": datetime.combine(day, time(8), LIMA).isoformat(),
                        "window_end": datetime.combine(day, time(12), LIMA).isoformat(),
                    },
                    sequence,
                )
            if "propose_appointment" not in results:
                slots = (results["query_available_slots"].get("data") or {}).get("slots", [])
                if not slots:
                    return self._answer(
                        "No tengo horarios libres esa mañana. ¿Te sirve otro día?",
                        "continue",
                        sequence,
                    )
                match = _NAME.search(text_value)
                return self._call(
                    "propose_appointment",
                    {
                        "full_name": match.group(1).title() if match else "Paciente",
                        "service_id": service["id"],
                        "location_id": location["id"],
                        "practitioner_id": slots[0]["practitioner_id"],
                        "start": slots[0]["start"],
                    },
                    sequence,
                )
            proposed = results["propose_appointment"]
            if proposed.get("status") != "success":
                return self._handoff("low_confidence", "La propuesta de cita falló.", sequence)
            return self._answer(
                f"Te propuse {service['name']} en {location['name']}. El personal de la "
                "clínica confirmará la cita.",
                "proposed",
                sequence,
            )

        def _information(self, text_value, results, sequence) -> AIMessage:
            if "get_reception_context" not in results:
                return self._call("get_reception_context", {}, sequence)
            data = results["get_reception_context"].get("data") or {}
            location = _pick(data.get("locations", []), text_value)
            hours = location.get("opening_hours") if location else None
            where = f" en {location['name']}" if location else ""
            return self._answer(
                f"Nuestro horario{where} es {json.dumps(hours, ensure_ascii=False)}. "
                "Los precios te los confirma recepción.",
                "continue",
                sequence,
            )

    return ReceptionFakeModel(messages=iter(()))


__all__ = ["FAKE_MODEL_NAME", "build_fake_model"]
