from datetime import datetime, time, timedelta, timezone
import random

from app.scheduling.availability import (
    Appointment,
    AvailabilityRule,
    ScheduleBlock,
    generate_slots,
)


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def rule(day_of_week, start_local, end_local):
    return AvailabilityRule(day_of_week, start_local, end_local)


def slot(y, mo, d, h, m, dur=30):
    s = utc(y, mo, d, h, m)
    return (s, s + timedelta(minutes=dur))


def test_30_min_service_on_15_minute_grid():
    rules = [rule(0, time(9, 0), time(10, 0))]
    expected = [
        slot(2026, 8, 10, 14, 0),
        slot(2026, 8, 10, 14, 15),
        slot(2026, 8, 10, 14, 30),
    ]
    got = generate_slots(rules, [], [], 30, utc(2026, 8, 10), utc(2026, 8, 11), "America/Lima")
    assert got == expected


def test_duration_not_divisible_by_15_uses_grid_and_fit_check():
    rules = [rule(0, time(9, 0), time(10, 0))]
    ws, we = utc(2026, 8, 10), utc(2026, 8, 11)
    expected_20 = [
        slot(2026, 8, 10, 14, 0, 20),
        slot(2026, 8, 10, 14, 15, 20),
        slot(2026, 8, 10, 14, 30, 20),
    ]
    expected_45 = [
        slot(2026, 8, 10, 14, 0, 45),
        slot(2026, 8, 10, 14, 15, 45),
    ]
    assert generate_slots(rules, [], [], 20, ws, we, "America/Lima") == expected_20
    assert generate_slots(rules, [], [], 45, ws, we, "America/Lima") == expected_45


def test_candidate_extending_past_availability_end_is_rejected():
    rules = [rule(0, time(9, 0), time(10, 0))]
    expected = [
        slot(2026, 8, 10, 14, 0, 40),
        slot(2026, 8, 10, 14, 15, 40),
    ]
    got = generate_slots(rules, [], [], 40, utc(2026, 8, 10), utc(2026, 8, 11), "America/Lima")
    assert got == expected


def test_schedule_block_removes_intersecting_candidates():
    rules = [rule(0, time(9, 0), time(12, 0))]
    blocks = [ScheduleBlock(utc(2026, 8, 10, 14, 30), utc(2026, 8, 10, 15, 15))]
    expected = [
        slot(2026, 8, 10, h, m)
        for h, m in [(14, 0), (15, 15), (15, 30), (15, 45), (16, 0), (16, 15), (16, 30)]
    ]
    got = generate_slots(rules, blocks, [], 30, utc(2026, 8, 10), utc(2026, 8, 11), "America/Lima")
    assert got == expected


def test_confirmed_appointment_removes_intersecting_candidates():
    rules = [rule(0, time(9, 0), time(12, 0))]
    appts = [Appointment(utc(2026, 8, 10, 14, 30), utc(2026, 8, 10, 15, 0), "confirmed")]
    expected = [
        slot(2026, 8, 10, h, m)
        for h, m in [(14, 0), (15, 0), (15, 15), (15, 30), (15, 45), (16, 0), (16, 15), (16, 30)]
    ]
    got = generate_slots(rules, [], appts, 30, utc(2026, 8, 10), utc(2026, 8, 11), "America/Lima")
    assert got == expected


def test_cancelled_appointment_does_not_block():
    rules = [rule(0, time(9, 0), time(12, 0))]
    appts = [Appointment(utc(2026, 8, 10, 14, 30), utc(2026, 8, 10, 15, 0), "cancelled")]
    expected = [
        slot(2026, 8, 10, h, m)
        for h in (14, 15, 16)
        for m in (0, 15, 30, 45)
        if not (h == 16 and m == 45)
    ]
    got = generate_slots(rules, [], appts, 30, utc(2026, 8, 10), utc(2026, 8, 11), "America/Lima")
    assert got == expected


def test_back_to_back_intervals_permitted():
    rules = [rule(0, time(9, 0), time(11, 0))]
    appts = [Appointment(utc(2026, 8, 10, 14, 0), utc(2026, 8, 10, 14, 30), "confirmed")]
    expected = [
        slot(2026, 8, 10, h, m)
        for h, m in [(14, 30), (14, 45), (15, 0), (15, 15), (15, 30)]
    ]
    got = generate_slots(rules, [], appts, 30, utc(2026, 8, 10), utc(2026, 8, 11), "America/Lima")
    assert got == expected


def test_multiple_recurring_windows_generate_candidates():
    rules = [rule(0, time(9, 0), time(12, 0)), rule(0, time(14, 0), time(17, 0))]
    expected = [
        slot(2026, 8, 10, h, m)
        for h in (14, 15, 16)
        for m in (0, 15, 30, 45)
        if not (h == 16 and m == 45)
    ]
    expected += [
        slot(2026, 8, 10, h, m)
        for h in (19, 20, 21)
        for m in (0, 15, 30, 45)
        if not (h == 21 and m == 45)
    ]
    got = generate_slots(rules, [], [], 30, utc(2026, 8, 10), utc(2026, 8, 11), "America/Lima")
    assert got == expected


def test_location_timezone_respected():
    rules = [rule(0, time(9, 0), time(10, 0))]
    got = generate_slots(rules, [], [], 30, utc(2026, 8, 10, 9, 0), utc(2026, 8, 10, 15, 0), "America/Lima")
    expected = [
        slot(2026, 8, 10, h, m)
        for h, m in [(14, 0), (14, 15), (14, 30)]
    ]
    assert got == expected


def test_dst_boundary_produces_exact_instants():
    rules = [rule(6, time(9, 0), time(10, 0))]
    expected = [
        slot(2026, 3, 1, 14, 0),
        slot(2026, 3, 1, 14, 15),
        slot(2026, 3, 1, 14, 30),
        slot(2026, 3, 8, 13, 0),
        slot(2026, 3, 8, 13, 15),
        slot(2026, 3, 8, 13, 30),
    ]
    got = generate_slots(rules, [], [], 30, utc(2026, 3, 1), utc(2026, 3, 9), "America/New_York")
    assert got == expected


def test_input_ordering_does_not_affect_output():
    rules = [rule(0, time(9, 0), time(12, 0)), rule(0, time(14, 0), time(17, 0))]
    blocks = [
        ScheduleBlock(utc(2026, 8, 10, 14, 30), utc(2026, 8, 10, 15, 0)),
        ScheduleBlock(utc(2026, 8, 10, 20, 0), utc(2026, 8, 10, 20, 30)),
    ]
    appts = [
        Appointment(utc(2026, 8, 10, 15, 30), utc(2026, 8, 10, 16, 0), "confirmed"),
        Appointment(utc(2026, 8, 10, 19, 30), utc(2026, 8, 10, 20, 0), "cancelled"),
    ]
    expected = [
        slot(2026, 8, 10, h, m)
        for h, m in [(14, 0), (15, 0), (16, 0), (16, 15), (16, 30)]
    ]
    expected += [
        slot(2026, 8, 10, h, m)
        for h, m in [(19, 0), (19, 15), (19, 30), (20, 30), (20, 45), (21, 0), (21, 15), (21, 30)]
    ]
    rng = random.Random(20260810)
    for _ in range(10):
        sr, sb, sa = list(rules), list(blocks), list(appts)
        rng.shuffle(sr)
        rng.shuffle(sb)
        rng.shuffle(sa)
        got = generate_slots(sr, sb, sa, 30, utc(2026, 8, 10), utc(2026, 8, 11), "America/Lima")
        assert got == expected
        assert all(got[i] < got[i + 1] for i in range(len(got) - 1))


def test_output_stable_and_chronologically_sorted():
    rules = [rule(0, time(9, 0), time(12, 0)), rule(3, time(14, 0), time(17, 0))]
    ws, we = utc(2026, 8, 10), utc(2026, 8, 14)
    first = generate_slots(rules, [], [], 30, ws, we, "America/Lima")
    second = generate_slots(rules, [], [], 30, ws, we, "America/Lima")
    assert first == second
    assert first == sorted(first)
    assert all(first[i] < first[i + 1] for i in range(len(first) - 1))
