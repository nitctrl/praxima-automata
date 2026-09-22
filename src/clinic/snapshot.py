"""Immutable, public-only operational snapshot. No legacy fixture coercion."""

import unicodedata
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(
            c if c.isalnum() or unicodedata.category(c).startswith("M") else " " for c in value
        ).split()
    )


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Effective(PublicModel):
    id: UUID
    effective_from: date
    effective_until: date | None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("Invalid effective interval")
        return self

    def effective(self, day: date) -> bool:
        return self.effective_from <= day and (
            self.effective_until is None or day < self.effective_until
        )


class Doctor(Effective):
    display_name: str = Field(min_length=1, max_length=200)
    aliases: tuple[str, ...]
    speciality: str
    languages: tuple[str, ...]
    short_public_bio: str
    accepts_new_patients: bool


class Service(Effective):
    name: str
    aliases: tuple[str, ...]
    short_approved_description: str
    appointment_required: bool


class Location(Effective):
    name: str
    address: str
    landmark: str | None
    directions: str | None
    map_url: str | None
    parking_information: str | None


class Fee(Effective):
    doctor_id: UUID
    service_id: UUID
    current_fee: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Hours(PublicModel):
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def ordered_hours(self) -> Self:
        if self.start_time.tzinfo or self.end_time.tzinfo or self.end_time <= self.start_time:
            raise ValueError("Split overnight hours explicitly; use local times")
        return self


class Weekly(Effective, Hours):
    doctor_id: UUID | None
    location_id: UUID
    day_of_week: int = Field(ge=0, le=6)
    availability_type: Literal["clinic_hours", "consultation", "no_walk_ins"]


class Special(Hours):
    id: UUID
    doctor_id: UUID | None
    location_id: UUID
    schedule_date: date


class ExceptionHours(PublicModel):
    id: UUID
    doctor_id: UUID | None
    location_id: UUID
    exception_date: date
    status: Literal["available", "unavailable", "modified_hours"]
    start_time: time | None
    end_time: time | None
    public_message: str

    @model_validator(mode="after")
    def valid_hours(self) -> Self:
        if self.status == "unavailable":
            if self.start_time is not None or self.end_time is not None:
                raise ValueError("Unavailable exceptions cannot contain hours")
        elif self.start_time is None or self.end_time is None:
            raise ValueError("Modified hours require both boundaries")
        else:
            Hours(start_time=self.start_time, end_time=self.end_time)
        return self


class Notice(PublicModel):
    id: UUID
    location_id: UUID | None
    doctor_id: UUID | None
    service_id: UUID | None
    notice_type: Literal[
        "closure",
        "doctor_unavailable",
        "service_unavailable",
        "no_walk_ins",
        "information",
        "manual_closure",
    ]
    public_message: str = Field(min_length=1, max_length=2000)
    starts_at: datetime
    expires_at: datetime
    priority: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if (
            self.starts_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.starts_at
        ):
            raise ValueError("Notices require an ordered aware interval")
        return self


class FAQ(Effective):
    category: str
    canonical_question: str
    alternative_phrasings: tuple[str, ...]
    approved_answer: str


class DocumentSection(PublicModel):
    """One staff-reviewed passage of clinic prose, tagged so retrieval can filter it."""

    id: UUID
    document_id: UUID
    document_title: str = Field(min_length=1, max_length=200)
    document_version: int = Field(ge=1)
    topic: str = Field(max_length=100)
    heading: str = Field(max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    doctor_id: UUID | None
    keywords: tuple[str, ...]


class Snapshot(PublicModel):
    schema_version: Literal[2, 3]
    clinic_id: UUID
    name: str
    timezone: str
    default_language: str
    supported_languages: tuple[str, ...]
    greeting: str
    emergency_message: str = Field(min_length=1, max_length=2000)
    fallback_message: str
    transfer_enabled: Literal[False]
    doctors: tuple[Doctor, ...]
    services: tuple[Service, ...]
    locations: tuple[Location, ...]
    doctor_services: tuple[Fee, ...]
    weekly_schedules: tuple[Weekly, ...]
    special_date_schedules: tuple[Special, ...]
    schedule_exceptions: tuple[ExceptionHours, ...]
    temporary_notices: tuple[Notice, ...]
    approved_faqs: tuple[FAQ, ...]
    # Reviewed prose only: raw extracted text and storage references never reach a caller.
    document_sections: tuple[DocumentSection, ...] = ()

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def references_and_conflicts(self) -> Self:
        if self.default_language not in self.supported_languages:
            raise ValueError("Default language must be supported")
        doctors = {row.id for row in self.doctors}
        services = {row.id for row in self.services}
        locations = {row.id for row in self.locations}
        if not locations or not any(row.doctor_id is None for row in self.weekly_schedules):
            raise ValueError("Published location and clinic hours are required")
        collections = (
            self.doctors,
            self.services,
            self.locations,
            self.doctor_services,
            self.weekly_schedules,
            self.special_date_schedules,
            self.schedule_exceptions,
            self.temporary_notices,
            self.approved_faqs,
            self.document_sections,
        )
        if sum(len(section.text) for section in self.document_sections) > 200_000 or len(
            self.document_sections
        ) > 200:
            raise ValueError("Published documents exceed the supported size")
        for collection in collections:
            if len({r.id for r in collection}) != len(collection) or len(collection) > 2000:
                raise ValueError("Duplicate identifiers or excessive snapshot size")
            for row in collection:
                for field, allowed in [
                    ("doctor_id", doctors),
                    ("service_id", services),
                    ("location_id", locations),
                ]:
                    ref = getattr(row, field, None)
                    if ref is not None and ref not in allowed:
                        raise ValueError("Snapshot reference is missing or inactive")
        for i, row in enumerate(self.weekly_schedules):
            for other in self.weekly_schedules[i + 1 :]:
                if (
                    (row.doctor_id, row.location_id, row.day_of_week)
                    == (other.doctor_id, other.location_id, other.day_of_week)
                    and max(row.start_time, other.start_time) < min(row.end_time, other.end_time)
                    and max(row.effective_from, other.effective_from)
                    < min(row.effective_until or date.max, other.effective_until or date.max)
                ):
                    raise ValueError("Overlapping weekly schedules")
        for i, fee in enumerate(self.doctor_services):
            for other_fee in self.doctor_services[i + 1 :]:
                if (fee.doctor_id, fee.service_id) == (
                    other_fee.doctor_id,
                    other_fee.service_id,
                ) and max(fee.effective_from, other_fee.effective_from) < min(
                    fee.effective_until or date.max, other_fee.effective_until or date.max
                ):
                    raise ValueError("Overlapping fees")
        for notice in self.temporary_notices:
            if notice.notice_type == "doctor_unavailable" and notice.doctor_id is None:
                raise ValueError("Doctor notice requires a doctor")
            if notice.notice_type == "service_unavailable" and notice.service_id is None:
                raise ValueError("Service notice requires a service")
        date_groups: tuple[tuple[Special | ExceptionHours, ...], ...] = (
            self.special_date_schedules,
            self.schedule_exceptions,
        )
        for date_group in date_groups:
            for i, dated in enumerate(date_group):
                for compared in date_group[i + 1 :]:
                    same_scope = (dated.doctor_id, dated.location_id) == (
                        compared.doctor_id,
                        compared.location_id,
                    )
                    same_day = getattr(
                        dated, "schedule_date", getattr(dated, "exception_date", None)
                    ) == (
                        getattr(
                            compared, "schedule_date", getattr(compared, "exception_date", None)
                        )
                    )
                    if (
                        same_scope
                        and same_day
                        and (
                            dated.start_time is None
                            or compared.start_time is None
                            or dated.end_time is None
                            or compared.end_time is None
                            or max(dated.start_time, compared.start_time)
                            < min(dated.end_time, compared.end_time)
                        )
                    ):
                        raise ValueError("Conflicting date-specific schedules")
        return self
