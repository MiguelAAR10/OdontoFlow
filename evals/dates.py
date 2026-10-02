"""The one calendar rule shared by the scenario text and the fake model."""

from __future__ import annotations

from datetime import date, timedelta

_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def booking_day(anchor: date) -> date:
    """First Monday–Friday at least two days after ``anchor``."""
    day = anchor + timedelta(days=2)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def spanish_day(day: date) -> str:
    """``lunes 5 de octubre`` — how a patient in Lima names a day."""
    return f"{_WEEKDAYS[day.weekday()]} {day.day} de {_MONTHS[day.month - 1]}"
