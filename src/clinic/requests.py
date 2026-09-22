"""Request-only state machine. Model booleans are never evidence of confirmation."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self

from clinic.safety import classify
from clinic.snapshot import normalize


class RequestDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["appointment", "callback"]
    name: str = Field(min_length=1, max_length=100, repr=False)
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$", repr=False)
    is_new_patient: bool = True
    doctor_id: UUID | None = None
    service_id: UUID | None = None
    preferred_date: date | None = None
    preferred_time_start: time | None = None
    preferred_time_end: time | None = None
    requested_time: datetime | None = None
    reason_category: Literal[
        "appointment", "hours", "fees", "registration", "human_requested", "other_admin"
    ] = "human_requested"

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if (
            not value.strip()
            or len(value.split()) > 6
            or any(
                not (c.isalpha() or unicodedata.category(c).startswith("M") or c in " .'-")
                for c in value
            )
            or classify(value).route in {"medical", "emergency", "injection"}
        ):
            raise ValueError("Use a caller name only, not notes or instructions")
        return value.strip()

    @model_validator(mode="after")
    def valid_request(self) -> Self:
        if self.kind == "appointment" and (
            self.preferred_date is None or (self.doctor_id is None and self.service_id is None)
        ):
            raise ValueError("An appointment request requires a date and doctor or service")
        if self.preferred_time_start and self.preferred_time_start.tzinfo:
            raise ValueError("Use local time")
        if self.preferred_time_end and (
            self.preferred_time_end.tzinfo
            or self.preferred_time_start is None
            or self.preferred_time_end <= self.preferred_time_start
        ):
            raise ValueError("Invalid time interval")
        if self.requested_time and self.requested_time.tzinfo is None:
            raise ValueError("Callback time must include a timezone")
        return self


@dataclass
class PendingRequest:
    details: RequestDetails
    request_id: UUID
    revision: UUID
    text: str = field(repr=False)
    spoken_turn: int | None = None
    confirmed_turn: int | None = None


class ConfirmationState:
    """Only a trusted adapter calls readback_completed/user_turn after actual events.

    Neither method is a LiveKit function tool. Interrupted playback and any
    correction invalidate confirmation. Confirmation is session-local and exact.
    """

    def __init__(self) -> None:
        self.pending: PendingRequest | None = None
        self.turn = 0

    def prepare(
        self, details: RequestDetails, description: str, language: str = "en-IN"
    ) -> PendingRequest:
        text = (
            (
                f"नाम {details.name}, संपर्क नंबर {details.phone}। {description} "
                "यह केवल अनुरोध है, पक्की अपॉइंटमेंट नहीं। क्या यह जानकारी सही है?"
            )
            if language == "hi-IN"
            else (
                f"Request for {details.name}, callback {details.phone}. {description} "
                "This is a request, not a confirmed appointment. Are these details correct?"
            )
        )
        self.pending = PendingRequest(details, uuid4(), uuid4(), text)
        return self.pending

    def readback_completed(self, revision: UUID, text: str, *, interrupted: bool) -> None:
        pending = self.pending
        if (
            pending is None
            or pending.revision != revision
            or interrupted
            or not re.fullmatch(re.escape(pending.text), text)
        ):
            if pending:
                pending.spoken_turn = pending.confirmed_turn = None
            return
        pending.spoken_turn = self.turn

    def user_turn(self, text: str) -> bool:
        self.turn += 1
        pending = self.pending
        if pending is None:
            return False
        yes = normalize(text) in {
            "yes",
            "yes correct",
            "correct",
            "confirm",
            "haan",
            "हाँ",
            "हां",
            "जी हाँ",
        }
        if yes and pending.spoken_turn == self.turn - 1:
            pending.confirmed_turn = self.turn
            return True
        pending.spoken_turn = pending.confirmed_turn = None
        return False

    def confirmed(self) -> PendingRequest:
        if self.pending is None or self.pending.confirmed_turn != self.turn:
            raise ValueError("Completed readback and subsequent caller confirmation required")
        return self.pending

    def clear(self) -> None:
        self.pending = None


def readback_digest(pending: PendingRequest) -> str:
    # Correlation only; not stored as plaintext or used as a phone/name lookup hash.
    return hashlib.sha256(str(pending.revision).encode()).hexdigest()
