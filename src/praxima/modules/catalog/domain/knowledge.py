"""Deterministic structured administrative facts from one frozen publication."""

from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import Field

from praxima.modules.releases.domain.snapshot import (
    Doctor,
    Location,
    Notice,
    PublicModel,
    Service,
    Snapshot,
    normalize,
)

Entity = TypeVar("Entity", bound=Doctor | Service | Location)
Interval = tuple[datetime, datetime]


class Query(PublicModel):
    name: str = Field(default="", max_length=200)
    doctor: str = Field(default="", max_length=200)
    service: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=200)
    speciality: str = Field(default="", max_length=200)
    requested_date: str = Field(default="today", max_length=32)
    time_preference: Literal["any", "morning", "afternoon", "evening"] = "any"
    after: str = Field(default="", max_length=8)
    before: str = Field(default="", max_length=8)


class Result(PublicModel):
    status: Literal["success", "not_found", "ambiguous", "unavailable", "forbidden", "failed"]
    data: dict[str, Any] = Field(default_factory=dict)
    next_action: str = "none"


def match(rows: Sequence[Entity], query: str) -> list[Entity]:
    key = normalize(query)
    if not key:
        return list(rows)
    exact: list[Entity] = []
    partial: list[Entity] = []
    for row in rows:
        if query == str(row.id):
            exact.append(row)
            continue
        name = row.display_name if isinstance(row, Doctor) else row.name
        names = [normalize(name), *(normalize(a) for a in getattr(row, "aliases", ()))]
        if key in names:
            exact.append(row)
        elif any(set(key.split()) <= set(n.split()) for n in names):
            partial.append(row)
    return exact or partial


def intersect(left: Sequence[Interval], right: Sequence[Interval]) -> list[Interval]:
    return sorted(
        {(max(a, c), min(b, d)) for a, b in left for c, d in right if max(a, c) < min(b, d)}
    )


def subtract(windows: Sequence[Interval], blocked: Interval) -> list[Interval]:
    result: list[Interval] = []
    for start, end in windows:
        a, b = blocked
        if b <= start or a >= end:
            result.append((start, end))
        else:
            if start < a:
                result.append((start, a))
            if b < end:
                result.append((b, end))
    return result


class StructuredKnowledge:
    def __init__(self, snapshot: Snapshot, *, clock: Callable[[], datetime] | None = None) -> None:
        self.snapshot = snapshot
        self.zone = ZoneInfo(snapshot.timezone)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("Clock must be timezone-aware")
        return now.astimezone(self.zone)

    def day(self, text: str) -> date:
        today = self.now().date()
        relative = {"today": 0, "tomorrow": 1, "aaj": 0, "आज": 0}
        # 'kal' can mean yesterday or tomorrow; require clarification instead of guessing.
        day = (
            today + timedelta(days=relative[text]) if text in relative else date.fromisoformat(text)
        )
        if day < today or day > today + timedelta(days=366):
            raise ValueError("Date outside the supported administrative horizon")
        return day

    def instant(self, day: date, local_time: time) -> datetime:
        if local_time.tzinfo is not None:
            raise ValueError("Local time must not contain a timezone")
        naive = datetime.combine(day, local_time)
        candidates = set()
        for fold in (0, 1):
            value = naive.replace(tzinfo=self.zone, fold=fold).astimezone(timezone.utc)
            if value.astimezone(self.zone).replace(tzinfo=None) == naive:
                candidates.add(value)
        if len(candidates) != 1:
            raise ValueError("DST-ambiguous or nonexistent local time; contact reception")
        return candidates.pop()

    def _choice(self, rows: Sequence[Entity], query: str) -> Entity | Result:
        matches = match(rows, query)
        if not matches:
            return Result(status="not_found", next_action="ask_for_clarification")
        if len(matches) > 1:
            return Result(
                status="ambiguous",
                data={"matches": [self._identity(r) for r in matches]},
                next_action="ask_for_clarification",
            )
        return matches[0]

    @staticmethod
    def _identity(row: Doctor | Service | Location) -> dict[str, str]:
        return {
            "reference": str(row.id),
            "name": row.display_name if isinstance(row, Doctor) else row.name,
        }

    def _hours(self, day: date, location: UUID, doctor: UUID | None) -> tuple[list[Interval], str]:
        exceptions = [
            r
            for r in self.snapshot.schedule_exceptions
            if r.exception_date == day and r.location_id == location and r.doctor_id == doctor
        ]
        if exceptions:
            if any(r.status == "unavailable" for r in exceptions):
                return [], "schedule_exception"
            return [
                (self.instant(day, r.start_time), self.instant(day, r.end_time))
                for r in exceptions
                if r.start_time is not None and r.end_time is not None
            ], "schedule_exception"
        special = [
            r
            for r in self.snapshot.special_date_schedules
            if r.schedule_date == day and r.location_id == location and r.doctor_id == doctor
        ]
        if special:
            return [
                (self.instant(day, r.start_time), self.instant(day, r.end_time)) for r in special
            ], "special_date_schedule"
        weekly = [
            r
            for r in self.snapshot.weekly_schedules
            if r.effective(day)
            and r.day_of_week == day.weekday()
            and r.location_id == location
            and r.doctor_id == doctor
        ]
        return [
            (self.instant(day, r.start_time), self.instant(day, r.end_time)) for r in weekly
        ], "weekly_schedule"

    def notices(
        self, day: date, location: UUID, doctor: UUID | None = None, service: UUID | None = None
    ) -> list[Notice]:
        start = self.instant(day, time.min)
        end = self.instant(day + timedelta(days=1), time.min)
        return sorted(
            [
                n
                for n in self.snapshot.temporary_notices
                if n.starts_at < end
                and n.expires_at > start
                and n.expires_at > self.now()
                and (n.location_id is None or n.location_id == location)
                and (n.doctor_id is None or n.doctor_id == doctor)
                and (n.service_id is None or n.service_id == service)
            ],
            key=lambda n: (n.notice_type == "manual_closure", n.priority, str(n.id)),
            reverse=True,
        )

    def _windows(
        self, day: date, location: UUID, doctor: UUID | None = None, service: UUID | None = None
    ) -> tuple[list[Interval], list[Notice], str]:
        windows, reason = self._hours(day, location, None)
        if doctor is not None:
            doctor_hours, doctor_reason = self._hours(day, location, doctor)
            windows = intersect(windows, doctor_hours)
            if doctor_reason != "weekly_schedule":
                reason = doctor_reason
        notices = self.notices(day, location, doctor, service)
        blocked = {"manual_closure", "closure", "doctor_unavailable", "service_unavailable"}
        for notice in notices:
            if notice.notice_type in blocked:
                windows = subtract(windows, (notice.starts_at, notice.expires_at))
        return windows, notices, reason

    def _walk_in_restrictions(
        self, day: date, location: UUID, doctor: UUID | None, notices: Sequence[Notice]
    ) -> list[Interval]:
        restrictions = [
            (n.starts_at, n.expires_at) for n in notices if n.notice_type == "no_walk_ins"
        ]
        for scope in {None, doctor}:
            # A date-specific schedule replaces the corresponding weekly schedule.
            overridden = any(
                r.doctor_id == scope and r.location_id == location and r.exception_date == day
                for r in self.snapshot.schedule_exceptions
            )
            overridden |= any(
                r.doctor_id == scope and r.location_id == location and r.schedule_date == day
                for r in self.snapshot.special_date_schedules
            )
            if not overridden:
                restrictions.extend(
                    (self.instant(day, row.start_time), self.instant(day, row.end_time))
                    for row in self.snapshot.weekly_schedules
                    if row.doctor_id == scope
                    and row.location_id == location
                    and row.effective(day)
                    and row.day_of_week == day.weekday()
                    and row.availability_type == "no_walk_ins"
                )
        return sorted(set(restrictions))

    def find_doctors(self, query: Query) -> Result:
        day = self.day(query.requested_date)
        rows = [r for r in self.snapshot.doctors if r.effective(day)]
        if query.speciality:
            rows = [r for r in rows if normalize(query.speciality) in normalize(r.speciality)]
        if query.service:
            selected = self._choice(
                [s for s in self.snapshot.services if s.effective(day)], query.service
            )
            if isinstance(selected, Result):
                return selected
            doctors = {
                f.doctor_id
                for f in self.snapshot.doctor_services
                if f.service_id == selected.id and f.effective(day)
            }
            rows = [r for r in rows if r.id in doctors]
        rows = match(rows, query.name)
        status: Literal["success", "not_found", "ambiguous"] = (
            "not_found" if not rows else "ambiguous" if query.name and len(rows) > 1 else "success"
        )
        return Result(
            status=status,
            data={
                "doctors": [
                    self._identity(r)
                    | {
                        "speciality": r.speciality,
                        "languages": list(r.languages),
                        "accepts_new_patients": r.accepts_new_patients,
                    }
                    for r in rows
                ]
            },
            next_action="ask_for_clarification" if status != "success" else "none",
        )

    def availability(self, query: Query) -> Result:
        day = self.day(query.requested_date)
        doctor = self._choice([d for d in self.snapshot.doctors if d.effective(day)], query.doctor)
        location = self._choice(
            [loc for loc in self.snapshot.locations if loc.effective(day)], query.location
        )
        if isinstance(doctor, Result):
            return doctor
        if isinstance(location, Result):
            return location
        service_id = None
        if query.service:
            selected = self._choice(
                [s for s in self.snapshot.services if s.effective(day)], query.service
            )
            if isinstance(selected, Result):
                return selected
            service_id = selected.id
            if not any(
                f.doctor_id == doctor.id and f.service_id == service_id and f.effective(day)
                for f in self.snapshot.doctor_services
            ):
                return Result(status="unavailable", next_action="contact_reception")
        windows, notices, reason = self._windows(day, location.id, doctor.id, service_id)
        bands = {
            "morning": (time(0), time(12)),
            "afternoon": (time(12), time(17)),
            "evening": (time(17), time(23, 59, 59)),
            "any": (time.min, time(23, 59, 59)),
        }
        start, end = bands[query.time_preference]
        if query.after:
            start = max(start, time.fromisoformat(query.after))
        if query.before:
            end = min(end, time.fromisoformat(query.before))
        if end <= start:
            raise ValueError("Time window must have increasing boundaries")
        windows = intersect(windows, [(self.instant(day, start), self.instant(day, end))])
        if day == self.now().date():
            windows = intersect(
                windows,
                [
                    (
                        self.now().astimezone(timezone.utc),
                        self.instant(day + timedelta(days=1), time.min),
                    )
                ],
            )
        restricted = intersect(
            windows, self._walk_in_restrictions(day, location.id, doctor.id, notices)
        )
        return Result(
            status="success",
            data={
                "doctor": self._identity(doctor),
                "location": self._identity(location),
                "date": day.isoformat(),
                "timezone": self.snapshot.timezone,
                "availability": "published_hours" if windows else "unavailable",
                "hours": self._serialize(windows),
                "walk_in_restricted_hours": self._serialize(restricted),
                "schedule_source": reason,
                "notices": [n.public_message for n in notices],
                "appointment_confirmed": False,
                "booking_policy": "Working hours are not slots. Staff must confirm requests.",
            },
        )

    def _serialize(self, windows: Sequence[Interval]) -> list[dict[str, str]]:
        return [
            {
                "start": a.astimezone(self.zone).isoformat(),
                "end": b.astimezone(self.zone).isoformat(),
            }
            for a, b in windows
        ]

    def current_status(self, location_name: str = "", requested_datetime: str = "") -> Result:
        now = (
            datetime.fromisoformat(requested_datetime.replace("Z", "+00:00"))
            if requested_datetime
            else self.now()
        )
        if now.tzinfo is None:
            raise ValueError("An explicit requested datetime must include its UTC offset")
        now = now.astimezone(self.zone)
        if requested_datetime and now < self.now():
            raise ValueError("Historical status lookup is not supported by this tool")
        self.day(now.date().isoformat())
        location = self._choice(
            [loc for loc in self.snapshot.locations if loc.effective(now.date())], location_name
        )
        if isinstance(location, Result):
            return location
        windows, notices, reason = self._windows(now.date(), location.id)
        active = [n for n in notices if n.starts_at <= now < n.expires_at]
        restrictions = self._walk_in_restrictions(now.date(), location.id, None, notices)
        return Result(
            status="success",
            data={
                "status": "open" if any(a <= now < b for a, b in windows) else "closed",
                "local_time": now.isoformat(),
                "timezone": self.snapshot.timezone,
                "hours": self._serialize(windows),
                "schedule_source": reason,
                "notices": [n.public_message for n in active],
                "walk_ins_restricted": any(a <= now < b for a, b in restrictions),
            },
        )

    def fee(self, query: Query) -> Result:
        day = self.day(query.requested_date)
        if not query.doctor and not query.service:
            return Result(status="ambiguous", next_action="ask_for_doctor_or_service")
        fees = [f for f in self.snapshot.doctor_services if f.effective(day)]
        active_doctors = [d for d in self.snapshot.doctors if d.effective(day)]
        active_services = [s for s in self.snapshot.services if s.effective(day)]
        fees = [
            f
            for f in fees
            if f.doctor_id in {d.id for d in active_doctors}
            and f.service_id in {s.id for s in active_services}
        ]
        selections: list[tuple[str, Sequence[Doctor | Service], str]] = [
            (query.doctor, active_doctors, "doctor_id"),
            (query.service, active_services, "service_id"),
        ]
        for name, rows, field in selections:
            if name:
                chosen = self._choice(rows, name)
                if isinstance(chosen, Result):
                    return chosen
                fees = [f for f in fees if getattr(f, field) == chosen.id]
        if len(fees) != 1:
            return Result(
                status="ambiguous" if fees else "unavailable",
                next_action="ask_for_doctor_or_service" if fees else "contact_reception",
            )
        f = fees[0]
        return Result(
            status="success",
            data={"amount": str(f.current_fee), "currency": f.currency, "date": day.isoformat()},
        )

    def service_information(self, query: Query) -> Result:
        day = self.day(query.requested_date)
        service = self._choice(
            [s for s in self.snapshot.services if s.effective(day)], query.service
        )
        if isinstance(service, Result):
            return service
        fees = [
            f
            for f in self.snapshot.doctor_services
            if f.effective(day) and f.service_id == service.id
        ]
        doctors = [
            d
            for d in self.snapshot.doctors
            if d.effective(day) and d.id in {f.doctor_id for f in fees}
        ]
        return Result(
            status="success",
            data=self._identity(service)
            | {
                "description": service.short_approved_description,
                "appointment_required": service.appointment_required,
                "doctors": [self._identity(d) for d in doctors],
                "fees": [
                    {
                        "doctor_reference": str(f.doctor_id),
                        "amount": str(f.current_fee),
                        "currency": f.currency,
                    }
                    for f in fees
                    if f.doctor_id in {d.id for d in doctors}
                ],
                "appointment_confirmed": False,
            },
        )

    def location(self, query: Query) -> Result:
        day = self.day(query.requested_date)
        location = self._choice(
            [loc for loc in self.snapshot.locations if loc.effective(day)], query.location
        )
        if isinstance(location, Result):
            return location
        return Result(
            status="success",
            data=self._identity(location)
            | {
                "address": location.address,
                "landmark": location.landmark,
                "directions": location.directions,
                "map_url": location.map_url,
                "parking_information": location.parking_information,
            },
        )

    def faq(self, question: str, category: str = "") -> Result:
        key = normalize(question)
        rows = [
            f
            for f in self.snapshot.approved_faqs
            if f.effective(self.now().date())
            and (not category or normalize(category) == normalize(f.category))
            and key
            in {normalize(f.canonical_question), *(normalize(q) for q in f.alternative_phrasings)}
        ]
        if not key or len(rows) != 1:
            return Result(
                status="unavailable",
                data={"reason": "insufficient_information"},
                next_action="contact_reception",
            )
        return Result(status="success", data={"answer": rows[0].approved_answer})
