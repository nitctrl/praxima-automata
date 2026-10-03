"""Bookable slots from published hours: one pure function shared by the API and the voice agent.

No I/O and no platform imports (the voice worker uses it too), so callers and staff always see
the same open slots for the same hours, bookings and settings.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr


@dataclass(frozen=True)
class WeeklyHours:
    """One recurring opening window, e.g. RRULE:FREQ=WEEKLY;BYDAY=MO,WE 10:00-13:00."""

    rrule: str
    start: time
    end: time


@dataclass(frozen=True)
class DayException:
    """A dated override: closed all day, or open only between `start` and `end`."""

    day: date
    available: bool
    start: time | None = None
    end: time | None = None


def occurs_on(rule: str, day: date) -> bool:
    start = datetime.combine(day - timedelta(days=400), time())
    found = rrulestr(rule, dtstart=start).between(
        datetime.combine(day, time()), datetime.combine(day, time(23, 59)), inc=True
    )
    return bool(found)


def day_windows(
    rules: Sequence[WeeklyHours], exceptions: Sequence[DayException], day: date
) -> list[tuple[time, time]]:
    """Opening windows on one day; a dated exception replaces the weekly hours."""
    exception = next((x for x in exceptions if x.day == day), None)
    if exception is not None:
        if exception.available and exception.start and exception.end:
            return [(exception.start, exception.end)]
        return []
    return sorted((r.start, r.end) for r in rules if r.start < r.end and occurs_on(r.rrule, day))


def open_slots(
    *,
    rules: Sequence[WeeklyHours],
    exceptions: Sequence[DayException],
    timezone: str,
    slot_minutes: int,
    busy: Iterable[tuple[datetime, datetime]],
    now: datetime,
    first_day: date,
    days: int,
    notice_minutes: int = 0,
    horizon_days: int | None = None,
    limit: int | None = None,
) -> list[datetime]:
    """Slot starts (aware, in `timezone`) inside opening hours that overlap nothing busy.

    Slots are aligned to each window's start, end inside it, begin at least `notice_minutes`
    from `now` and no later than `horizon_days` ahead.
    """
    zone = ZoneInfo(timezone)
    length = timedelta(minutes=slot_minutes)
    earliest = now + timedelta(minutes=notice_minutes)
    latest = now + timedelta(days=horizon_days) if horizon_days is not None else None
    taken = sorted(busy)
    found: list[datetime] = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        for opens, closes in day_windows(rules, exceptions, day):
            start = datetime.combine(day, opens, zone)
            end = datetime.combine(day, closes, zone)
            while start + length <= end:
                stop = start + length
                if (
                    start >= earliest
                    and (latest is None or start <= latest)
                    and not any(b_start < stop and start < b_end for b_start, b_end in taken)
                ):
                    found.append(start)
                    if limit is not None and len(found) >= limit:
                        return found
                start = stop
    return found
