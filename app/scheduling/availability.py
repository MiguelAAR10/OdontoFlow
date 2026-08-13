"""Deterministic availability slot generation core.

Pure module: stdlib only (``datetime``, ``zoneinfo``). No persistence, no
network, no LLM, no side effects. Given recurring availability rules,
exceptional schedule blocks, existing appointments, the canonical service
duration and a requested UTC window, it returns the strictly chronological
list of bookable intervals.

All intervals use half-open ``[start, end)`` semantics, so two intervals that
touch (``end_a == start_b``) do not intersect and back-to-back bookings are
valid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

GRID_MINUTES = 15


@dataclass(frozen=True)
class AvailabilityRule:
    """Recurring working window, evaluated in the location's time zone.

    ``day_of_week`` is 0=Monday .. 6=Sunday. ``start_local``/``end_local`` are
    wall-clock times and the window is half-open ``[start_local, end_local)``.
    """

    day_of_week: int
    start_local: time
    end_local: time


@dataclass(frozen=True)
class ScheduleBlock:
    """Exceptional closed (non-bookable) UTC interval, half-open."""

    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class Appointment:
    """Existing appointment. Only ``state == 'confirmed'`` blocks availability."""

    start_utc: datetime
    end_utc: datetime
    state: str


def _as_rule(value) -> AvailabilityRule:
    if isinstance(value, AvailabilityRule):
        return value
    if isinstance(value, (tuple, list)):
        return AvailabilityRule(*value)
    return AvailabilityRule(value.day_of_week, value.start_local, value.end_local)


def _as_block(value) -> ScheduleBlock:
    if isinstance(value, ScheduleBlock):
        return value
    if isinstance(value, (tuple, list)):
        return ScheduleBlock(*value)
    return ScheduleBlock(value.start_utc, value.end_utc)


def _as_appointment(value) -> Appointment:
    if isinstance(value, Appointment):
        return value
    if isinstance(value, (tuple, list)):
        return Appointment(*value)
    return Appointment(value.start_utc, value.end_utc, value.state)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def generate_slots(
    rules,
    blocks,
    appointments,
    duration_minutes,
    window_start,
    window_end,
    timezone,
):
    """Return bookable ``(start_utc, end_utc)`` intervals for the request.

    Candidate starts are aligned to a 15-minute grid evaluated on the location's
    wall clock. An interval is bookable when it fits wholly inside a recurring
    availability window, lies inside ``[window_start, window_end)``, and
    intersects neither a schedule block nor a confirmed appointment.

    Returns a strictly chronological, sorted, deduplicated list of tuples.
    Input ordering of ``rules``, ``blocks`` and ``appointments`` never affects
    the output.
    """
    rules = [_as_rule(value) for value in rules]
    blocks = [(_to_utc(_as_block(value).start_utc), _to_utc(_as_block(value).end_utc)) for value in blocks]
    confirmed = [
        (_to_utc(_as_appointment(value).start_utc), _to_utc(_as_appointment(value).end_utc))
        for value in appointments
        if _as_appointment(value).state == "confirmed"
    ]

    tz = ZoneInfo(timezone)
    utc_start = _to_utc(window_start)
    utc_end = _to_utc(window_end)
    duration = timedelta(minutes=duration_minutes)

    if duration_minutes <= 0 or utc_end <= utc_start:
        return []

    candidates = set()
    day = utc_start.date()
    last_day = utc_end.date()
    while day <= last_day:
        first_local = datetime(day.year, day.month, day.day, tzinfo=UTC).astimezone(tz).date()
        local_dates = {first_local, first_local + timedelta(days=1)}
        for local_date in local_dates:
            for rule in rules:
                if rule.day_of_week != local_date.weekday():
                    continue
                start_min = _time_to_minutes(rule.start_local)
                end_min = _time_to_minutes(rule.end_local)
                tick = start_min
                while tick < end_min:
                    if tick + duration_minutes <= end_min:
                        local_start = datetime(
                            local_date.year, local_date.month, local_date.day,
                            tick // 60, tick % 60, tzinfo=tz,
                        )
                        start_utc = local_start.astimezone(UTC)
                        end_utc = start_utc + duration
                        if start_utc < utc_start or end_utc > utc_end:
                            tick += GRID_MINUTES
                            continue
                        if any(start_utc < block_end and block_start < end_utc for block_start, block_end in blocks):
                            tick += GRID_MINUTES
                            continue
                        if any(start_utc < appt_end and appt_start < end_utc for appt_start, appt_end in confirmed):
                            tick += GRID_MINUTES
                            continue
                        candidates.add((start_utc, end_utc))
                    tick += GRID_MINUTES
        day += timedelta(days=1)

    return sorted(candidates)
