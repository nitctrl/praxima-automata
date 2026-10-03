"""Writes for scheduling: settings, staff bookings, confirm, cancel, reschedule, reveal.

Every change is audited. The database's exclusion constraint is the final guard against a
double booking; the open-slot check here gives a friendlier message first.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.integrations.google.calendar import GoogleCalendar, GoogleError, sign_state
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
from praxima.modules.scheduling.infrastructure.models import (
    Booking,
    BookingSettings,
    CalendarConnection,
    ExternalBusy,
)
from praxima.shared.db import outbox
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


# ------------------------------------------------------------------- calendars


async def start_calendar_connection(
    session: AsyncSession,
    actor: Actor,
    google: GoogleCalendar,
    *,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    now: float,
) -> str:
    """The Google consent URL for one bookable entry (opened by the person who owns it)."""
    require(actor, "calendars:manage")
    await bookable_entity(session, workspace_id, entity_id)
    state = sign_state(
        google.config.client_secret,
        {"ws": str(workspace_id), "entity": str(entity_id), "user": str(actor.user_id)},
        now,
    )
    return google.authorize_url(state)


async def _connection(session: AsyncSession, entity_id: uuid.UUID) -> CalendarConnection | None:
    row: CalendarConnection | None = await session.scalar(
        select(CalendarConnection).where(CalendarConnection.entity_id == entity_id)
    )
    return row


async def complete_calendar_connection(
    session: AsyncSession,
    actor: Actor,
    vault: Vault,
    google: GoogleCalendar,
    *,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    code: str,
) -> None:
    """Store the calendar's refresh token (encrypted) and queue its first sync."""
    require(actor, "calendars:manage")
    await bookable_entity(session, workspace_id, entity_id)
    try:
        tokens = await google.exchange_code(code)
        email = await google.account_email(tokens.access_token)
    except GoogleError as exc:
        raise ValidationFailed(f"Google refused the connection ({exc.code}). Try again.") from None
    if not tokens.refresh_token:
        raise ValidationFailed(
            "Google didn't grant offline access. Remove the app's access in "
            "your Google account, then connect again."
        )
    row = await _connection(session, entity_id)
    if row is None:
        row = CalendarConnection(
            workspace_id=workspace_id, entity_id=entity_id, created_by=actor.user_id
        )
        session.add(row)
        await session.flush()
    row.refresh_token_ciphertext = vault.seal(
        workspace_id, row.id, "google_refresh_token", tokens.refresh_token
    )
    row.pii_key_version = vault.version
    row.account_email = email
    row.status, row.error_code, row.last_synced_at = "active", None, None
    row.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, workspace_id, "calendar.connect", row.id)
    await outbox.enqueue(session, workspace_id, "calendar.sync_busy", {"entity_id": entity_id})
    upcoming = await session.scalars(
        select(Booking.id).where(
            Booking.resource_entity_id == entity_id,
            Booking.status == "confirmed",
            func.upper(Booking.slot) > utc_now(),
        )
    )
    for booking_id in upcoming:  # bookings made before connecting appear too
        await outbox.enqueue(
            session, workspace_id, "calendar.sync_booking", {"booking_id": booking_id}
        )


async def disconnect_calendar(
    session: AsyncSession,
    actor: Actor,
    vault: Vault,
    google: GoogleCalendar | None,
    *,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
) -> None:
    """Revoke Google's access and forget the token and the busy time it read."""
    require(actor, "calendars:manage")
    row = await _connection(session, entity_id)
    if row is None or row.status == "revoked":
        raise NotFound("No calendar is connected.")
    token = vault.open(
        workspace_id,
        row.id,
        "google_refresh_token",
        row.refresh_token_ciphertext,
        row.pii_key_version,
    )
    if token and google is not None:
        await google.revoke(token)
    row.status, row.refresh_token_ciphertext, row.pii_key_version = "revoked", None, None
    row.updated_by = actor.user_id
    await session.execute(delete(ExternalBusy).where(ExternalBusy.entity_id == entity_id))
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, workspace_id, "calendar.disconnect", row.id)


class JobFailed(Exception):
    def __init__(self, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code, self.retryable = code, retryable


async def _access_token(
    session: AsyncSession, vault: Vault, google: GoogleCalendar, row: CalendarConnection
) -> str:
    token = vault.open(
        row.workspace_id,
        row.id,
        "google_refresh_token",
        row.refresh_token_ciphertext,
        row.pii_key_version,
    )
    if not token:
        raise JobFailed("no_token", retryable=False)
    try:
        return await google.access_token(token)
    except GoogleError as exc:
        _calendar_error(row, exc)
        raise


def _calendar_error(row: CalendarConnection, exc: GoogleError) -> None:
    if not exc.retryable:  # access revoked in Google, or refused: staff must reconnect
        row.status, row.error_code = "error", exc.code


async def _event(session: AsyncSession, vault: Vault, booking: Booking) -> dict[str, object]:
    config = await require_config(session, booking.workspace_id)
    timezone = (await tenancy.get_workspace(session, booking.workspace_id)).timezone
    names = await catalog.entity_names(
        session, [booking.subject_entity_id] if booking.subject_entity_id else []
    )
    ws, version = booking.workspace_id, booking.pii_key_version
    name = vault.open(ws, booking.id, "subject_name", booking.subject_name_ciphertext, version)
    phone = vault.open(ws, booking.id, "phone", booking.phone_ciphertext, version)
    lines = [
        f"Phone: {phone}" if phone else None,
        f"For: {names[booking.subject_entity_id]}" if booking.subject_entity_id in names else None,
        "Booked by the voice agent on a call" if booking.source == "call" else "Booked by staff",
        f"Reference: {str(booking.id)[:8]}",
    ]
    return {
        "summary": f"{config.label}: {name or 'Booking'}",
        "description": "\n".join(line for line in lines if line),
        "start": {"dateTime": booking.slot.lower.isoformat(), "timeZone": timezone},  # type: ignore[union-attr]  # bounded
        "end": {"dateTime": booking.slot.upper.isoformat(), "timeZone": timezone},  # type: ignore[union-attr]
        "extendedProperties": {"private": {"praxima_booking": str(booking.id)}},
    }


async def _mark_synced(
    session: AsyncSession, booking_id: uuid.UUID, event_id: str | None, status: str | None
) -> None:
    """Calendar bookkeeping, not an edit: leaves row_version alone, so staff working on the
    booking at the same moment don't get a "changed by someone else" conflict."""
    await session.execute(
        update(Booking)
        .where(Booking.id == booking_id)
        .values(calendar_event_id=event_id, calendar_sync_status=status)
        .execution_options(synchronize_session=False)
    )


async def sync_booking_to_calendar(
    session: AsyncSession, vault: Vault, google: GoogleCalendar, booking_id: uuid.UUID
) -> None:
    """Confirmed → event created or moved; otherwise its event is removed."""
    booking = await session.get(Booking, booking_id)
    if booking is None:
        return
    row = await _connection(session, booking.resource_entity_id)
    if row is None or row.status != "active":
        return
    access = await _access_token(session, vault, google, row)
    try:
        if booking.status == "confirmed":
            event = await _event(session, vault, booking)
            event_id = await google.upsert_event(
                access, row.calendar_id, booking.calendar_event_id, event
            )
            await _mark_synced(session, booking.id, event_id, "synced")
        elif booking.calendar_event_id:
            await google.delete_event(access, row.calendar_id, booking.calendar_event_id)
            await _mark_synced(session, booking.id, None, "removed")
    except GoogleError as exc:
        _calendar_error(row, exc)
        await _mark_synced(session, booking.id, booking.calendar_event_id, "failed")
        raise


async def sync_busy_from_calendar(
    session: AsyncSession, vault: Vault, google: GoogleCalendar, entity_id: uuid.UUID
) -> None:
    """Replace the entry's cached busy time with Google's, for the booking window."""
    row = await _connection(session, entity_id)
    if row is None or row.status != "active":
        return
    settings = await booking_settings(session, row.workspace_id)
    now = utc_now()
    access = await _access_token(session, vault, google, row)
    try:
        busy = await google.busy(
            access, row.calendar_id, now, now + timedelta(days=settings.horizon_days + 1)
        )
    except GoogleError as exc:
        _calendar_error(row, exc)
        raise
    # Our own confirmed bookings come back as busy too; they're excluded from slots anyway.
    await session.execute(delete(ExternalBusy).where(ExternalBusy.entity_id == entity_id))
    for starts, ends in busy:
        if ends > starts:
            session.add(
                ExternalBusy(
                    workspace_id=row.workspace_id,
                    entity_id=entity_id,
                    busy=Range(starts, ends, bounds="[)"),
                    synced_at=now,
                )
            )
    row.last_synced_at, row.error_code = now, None
    await session.flush()


async def run_job(
    session: AsyncSession,
    vault: Vault,
    google: GoogleCalendar | None,
    *,
    kind: str,
    payload: dict[str, object],
) -> None:
    """One outbox job, inside its workspace's transaction. Raises JobFailed or GoogleError."""
    if kind.startswith("calendar.") and google is None:
        raise JobFailed("calendar_not_configured", retryable=False)
    try:
        if kind == "calendar.sync_booking":
            assert google is not None
            await sync_booking_to_calendar(
                session, vault, google, uuid.UUID(str(payload["booking_id"]))
            )
        elif kind == "calendar.sync_busy":
            assert google is not None
            await sync_busy_from_calendar(
                session, vault, google, uuid.UUID(str(payload["entity_id"]))
            )
        else:
            raise JobFailed("unknown_kind", retryable=False)
    except GoogleError as exc:
        raise JobFailed(exc.code, exc.retryable) from None


async def queue_busy_syncs(session: AsyncSession, older_than_minutes: int) -> int:
    """Queue a busy-time sync for each calendar not synced lately (cross-tenant, definer)."""
    queued = await session.scalar(
        text("SELECT scheduling.enqueue_busy_syncs(:m)"), {"m": older_than_minutes}
    )
    return int(queued or 0)


async def note_job_failure(
    session: AsyncSession, *, kind: str, payload: dict[str, object], code: str, retryable: bool
) -> None:
    """Record what staff need to see after a failed job (its own transaction rolled back)."""
    if not kind.startswith("calendar."):
        return
    entity_id: object = payload.get("entity_id")
    if kind == "calendar.sync_booking":
        booking = await session.get(Booking, uuid.UUID(str(payload["booking_id"])))
        if booking is None:
            return
        entity_id = booking.resource_entity_id
        await _mark_synced(session, booking.id, booking.calendar_event_id, "failed")
    if not retryable and entity_id is not None:
        row = await _connection(session, uuid.UUID(str(entity_id)))
        if row is not None and row.status == "active":
            row.status, row.error_code = "error", code[:60]
            await session.flush()
