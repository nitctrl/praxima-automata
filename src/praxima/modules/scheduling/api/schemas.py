"""Request and response models for scheduling endpoints."""

import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from praxima.modules import scheduling


class BookingSettingsOut(BaseModel):
    requires_confirmation: bool
    hold_minutes: int
    min_notice_minutes: int
    horizon_days: int
    slot_minutes: int
    slot_minutes_override: int | None
    row_version: int

    @classmethod
    def of(cls, view: scheduling.SettingsView) -> "BookingSettingsOut":
        return cls(**view.__dict__)


class BookingSettingsPatch(BaseModel):
    """Partial update. `row_version` is the value last read (0 before the first save)."""

    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=0)
    requires_confirmation: bool | None = None
    hold_minutes: int | None = Field(default=None, ge=5, le=1440)
    min_notice_minutes: int | None = Field(default=None, ge=0, le=10080)
    horizon_days: int | None = Field(default=None, ge=1, le=365)
    slot_minutes: int | None = Field(default=None, ge=0, le=480)  # 0: use the pack's

    def changes(self) -> scheduling.SettingsChanges:
        if self.slot_minutes is not None and 0 < self.slot_minutes < 5:
            raise ValueError("slot_minutes")
        return scheduling.SettingsChanges(
            requires_confirmation=self.requires_confirmation,
            hold_minutes=self.hold_minutes,
            min_notice_minutes=self.min_notice_minutes,
            horizon_days=self.horizon_days,
            slot_minutes=self.slot_minutes,
        )


class SlotOut(BaseModel):
    starts_at: datetime
    ends_at: datetime


class BookingOut(BaseModel):
    """`caller_name` is filled only in the audited search; elsewhere use /reveal."""

    id: uuid.UUID
    resource_entity_id: uuid.UUID
    resource_name: str
    subject_entity_id: uuid.UUID | None
    subject_name: str | None
    starts_at: datetime
    ends_at: datetime
    status: str
    hold_until: datetime | None
    source: str
    booked_by: str
    caller_name: str | None
    phone_last4: str | None
    cancel_reason: str | None
    confirmed_at: datetime | None
    created_at: datetime
    row_version: int
    calendar_sync_status: str | None  # pending, synced, failed, removed (Google Calendar)

    @classmethod
    def of(cls, view: scheduling.BookingView) -> "BookingOut":
        return cls(**view.__dict__)


class BookingSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts: AwareDatetime
    ends: AwareDatetime
    entity_id: uuid.UUID | None = None
    include_cancelled: bool = False


class BookingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_entity_id: uuid.UUID
    starts_at: AwareDatetime
    caller_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")
    subject_entity_id: uuid.UUID | None = None
    staff_note: str | None = Field(default=None, max_length=2000)

    def draft(self, idempotency_key: str | None) -> scheduling.BookingDraft:
        return scheduling.BookingDraft(
            resource_entity_id=self.resource_entity_id,
            starts_at=self.starts_at,
            caller_name=self.caller_name,
            phone=self.phone,
            subject_entity_id=self.subject_entity_id,
            staff_note=self.staff_note,
            idempotency_key=idempotency_key,
        )


class RowVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)


class CancelIn(RowVersionIn):
    reason: str | None = Field(default=None, max_length=300)


class RescheduleIn(RowVersionIn):
    starts_at: AwareDatetime


class RevealedBookingOut(BaseModel):
    caller_name: str | None
    phone: str | None
    staff_note: str | None


class CalendarConnectionOut(BaseModel):
    """`configured`: the server has Google OAuth set up; `status` none until connected."""

    configured: bool
    status: str  # none, active, error
    account_email: str | None = None
    error_code: str | None = None
    last_synced_at: datetime | None = None


class AuthorizeOut(BaseModel):
    authorize_url: str
