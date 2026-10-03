"""Scheduling tables: per-workspace booking settings and bookings (migration 0012).

Callers' names, numbers and staff notes are stored only as ciphertext (`*_ciphertext` +
`pii_key_version`) bound to workspace, booking and field. The last four digits of the phone
are kept in clear so staff can tell bookings apart without revealing the number.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, LargeBinary, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
from sqlalchemy.orm import Mapped, mapped_column

from praxima.shared.db.base import AuthoringMixin, Base, IdMixin, TenantMixin

SCHEMA = "scheduling"
WORKSPACES = "tenancy.workspaces.id"
LIVE = ("held", "confirmed")  # statuses that occupy a slot


class BookingSettings(AuthoringMixin, Base):
    __tablename__ = "booking_settings"
    __table_args__ = {"schema": SCHEMA}

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES), primary_key=True)
    requires_confirmation: Mapped[bool] = mapped_column(
        default=True, server_default=sql_text("true")
    )
    hold_minutes: Mapped[int] = mapped_column(default=30, server_default=sql_text("30"))
    min_notice_minutes: Mapped[int] = mapped_column(default=60, server_default=sql_text("60"))
    horizon_days: Mapped[int] = mapped_column(default=30, server_default=sql_text("30"))
    slot_minutes: Mapped[int | None]


class Booking(IdMixin, TenantMixin, AuthoringMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = {"schema": SCHEMA}

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    resource_entity_id: Mapped[uuid.UUID]
    subject_entity_id: Mapped[uuid.UUID | None]
    slot: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    status: Mapped[str] = mapped_column(Text)
    hold_until: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID | None]
    agent_id: Mapped[uuid.UUID | None]
    subject_name_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    phone_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    phone_last4: Mapped[str | None] = mapped_column(Text)
    staff_note_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    pii_key_version: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None]
    confirmed_by: Mapped[uuid.UUID | None]
    cancelled_at: Mapped[datetime | None]
    cancelled_by: Mapped[uuid.UUID | None]
