"""Open slots from published hours (pure; shared by the API and the voice agent)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from praxima.packs import loader
from praxima.packs.loader import Pack, PackError
from praxima.shared.kernel.slots import DayException, WeeklyHours, day_windows, open_slots

IST = ZoneInfo("Asia/Kolkata")
MONDAY = date(2026, 10, 5)
MORNINGS = [WeeklyHours("FREQ=WEEKLY;BYDAY=MO,WE", time(10), time(11))]
BEFORE = datetime(2026, 10, 1, 9, 0, tzinfo=IST)


def slots(**overrides):  # type: ignore[no-untyped-def]
    values = {
        "rules": MORNINGS,
        "exceptions": [],
        "timezone": "Asia/Kolkata",
        "slot_minutes": 15,
        "busy": [],
        "now": BEFORE,
        "first_day": MONDAY,
        "days": 1,
    }
    return open_slots(**(values | overrides))


def at(hour: int, minute: int = 0, day: date = MONDAY) -> datetime:
    return datetime.combine(day, time(hour, minute), IST)


def test_slots_fill_each_window_in_the_business_timezone():
    assert slots() == [at(10), at(10, 15), at(10, 30), at(10, 45)]
    assert slots()[0].utcoffset() == timedelta(hours=5, minutes=30)
    assert slots(slot_minutes=25) == [at(10), at(10, 25)]  # only whole slots fit
    assert slots(first_day=MONDAY + timedelta(days=1)) == []  # Tuesday: closed


def test_dated_exceptions_replace_the_weekly_hours():
    leave = [DayException(MONDAY, available=False)]
    late = [DayException(MONDAY, available=True, start=time(16), end=time(16, 30))]
    assert slots(exceptions=leave) == []
    assert slots(exceptions=late) == [at(16), at(16, 15)]
    assert day_windows(MORNINGS, late, MONDAY) == [(time(16), time(16, 30))]


def test_busy_time_notice_horizon_and_limit():
    busy = [(at(10, 10), at(10, 20))]  # overlaps the 10:00 and 10:15 slots
    assert slots(busy=busy) == [at(10, 30), at(10, 45)]
    assert slots(busy=[(at(10, 15), at(10, 30))]) == [at(10), at(10, 30), at(10, 45)]
    assert slots(now=at(10, 5), notice_minutes=20) == [at(10, 30), at(10, 45)]
    # Window: 5 days from Oct 1 covers Monday 5th but not Wednesday 7th.
    assert slots(days=3, horizon_days=5) == [at(10), at(10, 15), at(10, 30), at(10, 45)]
    assert slots(now=at(9), horizon_days=0) == []
    assert slots(days=3, limit=5) == [*slots(), at(10, day=MONDAY + timedelta(days=2))]


def test_packs_declare_what_is_bookable():
    clinic, estate = loader.load("clinic"), loader.load("real_estate")
    assert clinic.booking and clinic.booking.resource_types == ("doctor",)
    assert estate.booking and estate.booking.slot_minutes == 60
    assert {"find_open_slots", "book_slot"} <= set(clinic.tools)

    payload = clinic.payload()
    unbookable = {**payload, "booking": {**payload["booking"], "resource_types": ["service"]}}
    with pytest.raises(PackError, match="need opening hours"):
        Pack.from_payload(unbookable)
    unknown = {**payload, "booking": {**payload["booking"], "subject_types": ["room"]}}
    with pytest.raises(PackError, match="unknown entity type"):
        Pack.from_payload(unknown)

    # Packs without booking keep their exact payload, so registered checksums don't change.
    without = {k: v for k, v in payload.items() if k != "booking"}
    assert "booking" not in Pack.from_payload(without).payload()
