"""The seven and only seven Sales Agent V0 tools."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from sales_agent.gateway import BackendGateway
from sales_agent.schemas import AgentToolResult


def _tool_result(result: AgentToolResult) -> dict[str, Any]:
    """Keep backend success/error envelopes typed when handing them to a model."""
    return result.model_dump(mode="json")


def build_v0_tools(gateway: BackendGateway, *, conversation_id: int):
    """Build exactly the seven conversation-bound V0 tool wrappers."""
    from langchain.tools import tool

    @tool("get_reception_context")
    def get_reception_context(as_of: date | None = None) -> dict[str, Any]:
        """Read the authorized reception context for this conversation.

        Use this to understand the current conversation status, retained recent
        messages, available services and locations, and any pending booking
        proposal. The response contains no promotions or prices.

        Args:
            as_of: Optional calendar date used when reading the deterministic
                reception context.
        """
        arguments = {"as_of": as_of.isoformat()} if as_of is not None else {}
        return _tool_result(
            gateway.call_tool(
                "get_reception_context",
                conversation_id=conversation_id,
                arguments=arguments,
            )
        )

    @tool("list_services")
    def list_services() -> dict[str, Any]:
        """List active bookable services from the authorized clinic catalog.

        Use this before proposing an appointment so service identity and
        duration come from the canonical backend catalog.

        Args:
            None.
        """
        return _tool_result(
            gateway.call_tool(
                "list_services",
                conversation_id=conversation_id,
                arguments={},
            )
        )

    @tool("list_locations")
    def list_locations() -> dict[str, Any]:
        """List active clinic locations available to this conversation.

        Use this before querying availability so the location and timezone are
        selected from canonical backend data.

        Args:
            None.
        """
        return _tool_result(
            gateway.call_tool(
                "list_locations",
                conversation_id=conversation_id,
                arguments={},
            )
        )

    @tool("query_available_slots")
    def query_available_slots(
        service_id: int,
        location_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        """Query deterministic available appointment slots for a service/location.

        The backend validates service duration, timezone-aware boundaries,
        practitioner capability, working hours and conflicts. Never invent a
        slot or change the returned start/end values.

        Args:
            service_id: Canonical active service identifier.
            location_id: Canonical active clinic location identifier.
            window_start: Inclusive timezone-aware beginning of the search window.
            window_end: Exclusive timezone-aware end of the search window.
        """
        arguments = {
            "service_id": service_id,
            "location_id": location_id,
            "window_start": window_start,
            "window_end": window_end,
        }
        return _tool_result(
            gateway.call_tool(
                "query_available_slots",
                conversation_id=conversation_id,
                arguments=arguments,
            )
        )

    @tool("propose_appointment")
    def propose_appointment(
        full_name: str,
        service_id: int,
        location_id: int,
        practitioner_id: int,
        start: datetime,
    ) -> dict[str, Any]:
        """Create a pending appointment proposal for one exact returned slot.

        This does not confirm or book the appointment. Call it only after a
        contact has selected a slot returned by query_available_slots, and ask
        for explicit confirmation before calling confirm_appointment.

        Args:
            full_name: Contact's name used by the canonical profile service.
            service_id: Canonical service identifier from list_services.
            location_id: Canonical location identifier from list_locations.
            practitioner_id: Practitioner identifier returned with the selected slot.
            start: Timezone-aware start of the exact selected slot.
        """
        arguments = {
            "full_name": full_name,
            "service_id": service_id,
            "location_id": location_id,
            "practitioner_id": practitioner_id,
            "start": start,
        }
        return _tool_result(
            gateway.call_tool(
                "propose_appointment",
                conversation_id=conversation_id,
                arguments=arguments,
            )
        )

    @tool("confirm_appointment")
    def confirm_appointment(proposal_id: int, confirmation_token: UUID) -> dict[str, Any]:
        """Confirm a pending appointment proposal after explicit contact approval.

        Call this only when the contact has clearly affirmed the exact pending
        proposal. The canonical backend performs the final availability and
        idempotency checks; this tool cannot cancel or reschedule appointments.

        Args:
            proposal_id: Canonical pending appointment proposal identifier.
            confirmation_token: Exact confirmation token returned by the proposal.
        """
        return _tool_result(
            gateway.call_tool(
                "confirm_appointment",
                conversation_id=conversation_id,
                arguments={
                    "proposal_id": proposal_id,
                    "confirmation_token": confirmation_token,
                },
            )
        )

    @tool("request_human_handoff")
    def request_human_handoff(
        reason_code: Literal[
            "requested_by_contact",
            "urgent_symptoms",
            "complaint",
            "pricing_exception",
            "clinical_case",
            "low_confidence",
            "other",
        ],
        reason_summary: str,
    ) -> dict[str, Any]:
        """Transfer this conversation to human reception and stop automation.

        Use this for urgent symptoms, clinical questions, complaints, pricing
        exceptions, low confidence, or whenever the contact asks for a person.
        Once accepted, the canonical backend blocks all Sales Agent tools until
        an authorized operator resumes the conversation.

        Args:
            reason_code: Controlled reason for the human handoff.
            reason_summary: Concise safe summary of why human reception is needed.
        """
        return _tool_result(
            gateway.call_tool(
                "request_human_handoff",
                conversation_id=conversation_id,
                arguments={
                    "reason_code": reason_code,
                    "reason_summary": reason_summary,
                },
            )
        )

    return (
        get_reception_context,
        list_services,
        list_locations,
        query_available_slots,
        propose_appointment,
        confirm_appointment,
        request_human_handoff,
    )


__all__ = ["build_v0_tools"]
