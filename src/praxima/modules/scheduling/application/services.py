"""Writes for scheduling: settings, staff bookings, confirm, cancel, reschedule, reveal.

Every change is audited. The database's exclusion constraint is the final guard against a
double booking; the open-slot check here gives a friendlier message first.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import audit, catalog, tenancy
from praxima.modules.engagement import Vault
from praxima.modules.iam import Actor, require
from praxima.modules.scheduling.application.selectors import (
    DEFAULTS,
    BookingView,
    bookable_entity,
    booking_settings,
    bookings_between,
    get_booking,
    open_slots_for,
    pending_confirmation,
    require_config,
)
from praxima.modules.scheduling.infrastructure.models import Booking, BookingSettings
from praxima.shared.db.base import utc_now
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import Conflict, FieldError, NotFound, ValidationFailed
from praxima.shared.kernel.ids import new_id

E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
TAKEN = "That slot was just taken. Pick another time."


async def _audit(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID, action: str, booking_id: uuid.UUID
) -> None:
    await audit.record(
        session,
        organization_id=await tenancy.organization_of(session, workspace_id),
        workspace_id=workspace_id,
        actor_id=actor.user_id,
        action=action,
        resource_type="booking",
        resource_id=booking_id,
    )


async def expire_holds(session: AsyncSession) -> None:
    """Holds past their time free their slot (so the exclusion constraint lets it go)."""
    await session.execute(
        update(Booking)
        .where(Booking.status == "held", Booking.hold_until <= utc_now())
        .values(status="expired")
        .execution_options(synchronize_session=False)
    )


# ------------------------------------------------------------------- settings


@dataclass(frozen=True)
class SettingsChanges:
    """PATCH fields; None means "leave unchanged" (slot_minutes 0 clears the override)."""

    requires_confirmation: bool | None = None
    hold_minutes: int | None = None
    min_notice_minutes: int | None = None
    horizon_days: int | None = None
    slot_minutes: int | None = None


async def update_settings(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    row_version: int,
    changes: SettingsChanges,
) -> None:
    require(actor, "bookings:settings")
    await require_config(session, workspace_id)
    row = await session.get(BookingSettings, workspace_id)
    if (row.row_version if row else 0) != row_version:
        raise Conflict("Booking settings were changed by someone else. Reload and try again.")
    if row is None:
        row = BookingSettings(workspace_id=workspace_id, created_by=actor.user_id, **DEFAULTS)
        session.add(row)
    for name in DEFAULTS:
        if (value := getattr(changes, name)) is not None:
            setattr(row, name, value)
    if changes.slot_minutes is not None:
        row.slot_minutes = changes.slot_minutes or None
    row.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await audit.record(
        session,
        organization_id=await tenancy.organization_of(session, workspace_id),
        workspace_id=workspace_id,
        actor_id=actor.user_id,
        action="booking_settings.update",
        resource_type="workspace",
        resource_id=workspace_id,
        change_diff={"requires_confirmation": row.requires_confirmation},
    )


# ------------------------------------------------------------------- bookings


@dataclass(frozen=True)
class BookingDraft:
    resource_entity_id: uuid.UUID
    starts_at: datetime
    caller_name: str
    phone: str | None = None
    subject_entity_id: uuid.UUID | None = None
    staff_note: str | None = None
    idempotency_key: str | None = None


async def _check_subject(
    session: AsyncSession, workspace_id: uuid.UUID, subject_id: uuid.UUID | None
) -> None:
    if subject_id is None:
        return
    config = await require_config(session, workspace_id)
    subject = await catalog.get_entity(session, subject_id)
    if subject.type not in config.subject_types:
        raise ValidationFailed(errors=[FieldError("subject_entity_id", "Not bookable for this.")])


async def _check_open(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    starts_at: datetime,
    ignore: uuid.UUID | None = None,
) -> int:
    """The slot length if `starts_at` is an open slot; otherwise 409 (taken) or 422."""
    if starts_at.tzinfo is None:
        raise ValidationFailed(errors=[FieldError("starts_at", "Include a time zone.")])
    timezone = (await tenancy.get_workspace(session, workspace_id)).timezone
    day = starts_at.astimezone(ZoneInfo(timezone)).date()
    slots = await open_slots_for(
        session, workspace_id, entity_id, first_day=day, days=1, ignore_booking=ignore
    )
    if starts_at not in slots:
        raise Conflict(
            "That time isn't open. Pick one of the open slots (inside published hours, "
            "not already booked, and within the booking window)."
        )
    return (await booking_settings(session, workspace_id)).slot_minutes


async def book(
    session: AsyncSession,
    actor: Actor,
    vault: Vault,
    *,
    workspace_id: uuid.UUID,
    draft: BookingDraft,
) -> uuid.UUID:
    """Staff book a slot for someone (confirmed at once). Idempotent with the same key."""
    require(actor, "bookings:write")
    if draft.idempotency_key:
        existing = await session.scalar(
            select(Booking.id).where(Booking.idempotency_key == draft.idempotency_key)
        )
        if existing is not None:
            return existing
    name = draft.caller_name.strip()
    if not name:
        raise ValidationFailed(errors=[FieldError("caller_name", "Enter the person's name.")])
    if draft.phone is not None and not E164.fullmatch(draft.phone):
        raise ValidationFailed(errors=[FieldError("phone", "Use E.164, e.g. +9198…")])
    await bookable_entity(session, workspace_id, draft.resource_entity_id)
    await _check_subject(session, workspace_id, draft.subject_entity_id)
    await expire_holds(session)
    minutes = await _check_open(session, workspace_id, draft.resource_entity_id, draft.starts_at)
    booking_id = new_id()
    now = utc_now()
    booking = Booking(
        id=booking_id,
        workspace_id=workspace_id,
        resource_entity_id=draft.resource_entity_id,
        subject_entity_id=draft.subject_entity_id,
        slot=Range(draft.starts_at, draft.starts_at + timedelta(minutes=minutes), bounds="[)"),
        status="confirmed",
        source="staff",
        subject_name_ciphertext=vault.seal(workspace_id, booking_id, "subject_name", name),
        phone_ciphertext=vault.seal(workspace_id, booking_id, "phone", draft.phone),
        phone_last4=draft.phone[-4:] if draft.phone else None,
        staff_note_ciphertext=vault.seal(
            workspace_id, booking_id, "staff_note", draft.staff_note or None
        ),
        pii_key_version=vault.version,
        idempotency_key=draft.idempotency_key,
        confirmed_at=now,
        confirmed_by=actor.user_id,
        created_by=actor.user_id,
        updated_by=actor.user_id,
    )
    with translate_db_errors(duplicate=TAKEN):
        session.add(booking)
        await session.flush()
    await _audit(session, actor, workspace_id, "booking.create", booking_id)
    return booking_id


async def _booking(session: AsyncSession, booking_id: uuid.UUID, row_version: int) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise NotFound("Booking not found.")
    if booking.row_version != row_version:
        raise Conflict("This booking was changed by someone else. Reload and try again.")
    return booking


async def confirm(
    session: AsyncSession, actor: Actor, *, booking_id: uuid.UUID, row_version: int
) -> None:
    """Staff confirm a slot the agent held for a caller."""
    require(actor, "bookings:write")
    booking = await _booking(session, booking_id, row_version)
    if booking.status == "confirmed":
        return
    if booking.status != "held" or (booking.hold_until and booking.hold_until <= utc_now()):
        raise Conflict("This hold has expired or was cancelled. Book the slot again if needed.")
    booking.status = "confirmed"
    booking.confirmed_at, booking.confirmed_by = utc_now(), actor.user_id
    booking.hold_until = None
    booking.updated_by = actor.user_id
    with translate_db_errors(duplicate=TAKEN):
        await session.flush()
    await _audit(session, actor, booking.workspace_id, "booking.confirm", booking.id)


async def cancel(
    session: AsyncSession,
    actor: Actor,
    *,
    booking_id: uuid.UUID,
    row_version: int,
    reason: str | None,
) -> None:
    require(actor, "bookings:write")
    booking = await _booking(session, booking_id, row_version)
    if booking.status in ("cancelled", "expired"):
        return
    if booking.status not in ("held", "confirmed"):
        raise Conflict("Only upcoming bookings can be cancelled.")
    booking.status = "cancelled"
    booking.cancel_reason = (reason or "").strip()[:300] or None
    booking.cancelled_at, booking.cancelled_by = utc_now(), actor.user_id
    booking.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, booking.workspace_id, "booking.cancel", booking.id)


async def reschedule(
    session: AsyncSession,
    actor: Actor,
    *,
    booking_id: uuid.UUID,
    row_version: int,
    starts_at: datetime,
) -> None:
    """Move an upcoming booking to another open slot of the same resource."""
    require(actor, "bookings:write")
    booking = await _booking(session, booking_id, row_version)
    if booking.status not in ("held", "confirmed"):
        raise Conflict("Only upcoming bookings can be moved.")
    await expire_holds(session)
    minutes = await _check_open(
        session, booking.workspace_id, booking.resource_entity_id, starts_at, ignore=booking.id
    )
    booking.slot = Range(starts_at, starts_at + timedelta(minutes=minutes), bounds="[)")
    booking.updated_by = actor.user_id
    with translate_db_errors(duplicate=TAKEN):
        await session.flush()
    await _audit(session, actor, booking.workspace_id, "booking.reschedule", booking.id)


@dataclass(frozen=True)
class RevealedBooking:
    caller_name: str | None
    phone: str | None
    staff_note: str | None


async def reveal(
    session: AsyncSession, actor: Actor, vault: Vault, *, booking_id: uuid.UUID
) -> RevealedBooking:
    """The full phone number and staff note. Every call is audited."""
    require(actor, "pii:reveal")
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise NotFound("Booking not found.")
    await _audit(session, actor, booking.workspace_id, "pii.reveal", booking.id)
    ws, version = booking.workspace_id, booking.pii_key_version
    return RevealedBooking(
        vault.open(ws, booking.id, "subject_name", booking.subject_name_ciphertext, version),
        vault.open(ws, booking.id, "phone", booking.phone_ciphertext, version),
        vault.open(ws, booking.id, "staff_note", booking.staff_note_ciphertext, version),
    )


async def list_bookings(
    session: AsyncSession,
    actor: Actor,
    vault: Vault,
    *,
    workspace_id: uuid.UUID,
    starts: datetime,
    ends: datetime,
    entity_id: uuid.UUID | None,
    include_cancelled: bool,
) -> list[BookingView]:
    """The schedule, with callers' names. Viewing names is audited once per request."""
    require(actor, "bookings:read")
    if ends <= starts or ends - starts > timedelta(days=42):
        raise ValidationFailed("Choose a range of up to six weeks.")
    views = await bookings_between(
        session,
        vault,
        starts=starts,
        ends=ends,
        entity_id=entity_id,
        include_cancelled=include_cancelled,
    )
    if views:
        await _audit(session, actor, workspace_id, "bookings.view_names", workspace_id)
    return views


async def to_confirm(
    session: AsyncSession, actor: Actor, vault: Vault, *, workspace_id: uuid.UUID
) -> list[BookingView]:
    """Holds waiting for staff, with callers' names (audited, like the schedule)."""
    require(actor, "bookings:read")
    views = await pending_confirmation(session, vault)
    if views:
        await _audit(session, actor, workspace_id, "bookings.view_names", workspace_id)
    return views


async def booking_detail(
    session: AsyncSession, actor: Actor, vault: Vault, booking_id: uuid.UUID
) -> BookingView:
    """One booking without personal data (names come from the audited search or reveal)."""
    require(actor, "bookings:read")
    return await get_booking(session, vault, booking_id)
