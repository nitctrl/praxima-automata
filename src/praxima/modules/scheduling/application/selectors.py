"""Reads for scheduling: settings, open slots and bookings. RLS limits all to the workspace."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import catalog, iam, tenancy
from praxima.modules.engagement import Vault
from praxima.modules.scheduling.infrastructure.models import Booking, BookingSettings
from praxima.shared.db.base import utc_now
from praxima.shared.errors import NotFound, ValidationFailed
from praxima.shared.kernel.slots import DayException, WeeklyHours, open_slots

DEFAULTS = {
    "requires_confirmation": True,
    "hold_minutes": 30,
    "min_notice_minutes": 60,
    "horizon_days": 30,
}


@dataclass(frozen=True)
class BookingConfig:
    """What the workspace's pack lets people book (None when the pack has no booking)."""

    label: str
    plural_label: str
    resource_types: list[str]
    subject_types: list[str]
    slot_minutes: int


@dataclass(frozen=True)
class SettingsView:
    requires_confirmation: bool
    hold_minutes: int
    min_notice_minutes: int
    horizon_days: int
    slot_minutes: int  # effective: the override, else the pack's
    slot_minutes_override: int | None
    row_version: int  # 0 until first saved


async def booking_config(session: AsyncSession, workspace_id: uuid.UUID) -> BookingConfig | None:
    booking = (await tenancy.installed_pack(session, workspace_id)).booking
    if booking is None:
        return None
    return BookingConfig(
        booking.label,
        booking.plural_label or f"{booking.label}s",
        list(booking.resource_types),
        list(booking.subject_types),
        booking.slot_minutes,
    )


async def require_config(session: AsyncSession, workspace_id: uuid.UUID) -> BookingConfig:
    config = await booking_config(session, workspace_id)
    if config is None:
        raise ValidationFailed(
            "This workspace's domain pack has no booking. Upgrade the pack in Settings."
        )
    return config


async def booking_settings(session: AsyncSession, workspace_id: uuid.UUID) -> SettingsView:
    config = await require_config(session, workspace_id)
    row = await session.get(BookingSettings, workspace_id)
    values: dict[str, Any] = dict(DEFAULTS)
    if row is not None:
        values = {name: getattr(row, name) for name in DEFAULTS}
    override = row.slot_minutes if row is not None else None
    return SettingsView(
        **values,
        slot_minutes=override or config.slot_minutes,
        slot_minutes_override=override,
        row_version=row.row_version if row is not None else 0,
    )


def _live(now: datetime) -> Any:
    """Bookings that occupy their slot: confirmed, or held and not yet expired."""
    return or_(
        Booking.status == "confirmed",
        and_(Booking.status == "held", Booking.hold_until > now),
    )


async def bookable_entity(
    session: AsyncSession, workspace_id: uuid.UUID, entity_id: uuid.UUID
) -> catalog.EntityView:
    config = await require_config(session, workspace_id)
    entity = await catalog.get_entity(session, entity_id)
    if entity.type not in config.resource_types:
        raise ValidationFailed(f"{entity.name} can't be booked.")
    return entity


async def open_slots_for(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    *,
    first_day: date,
    days: int,
    ignore_booking: uuid.UUID | None = None,
    now: datetime | None = None,
) -> list[datetime]:
    """Open slot starts of one bookable entity, from its published hours.

    Booked (confirmed or unexpired held) time is excluded, as are slots inside the minimum
    notice or beyond the booking window.
    """
    await bookable_entity(session, workspace_id, entity_id)
    settings = await booking_settings(session, workspace_id)
    timezone = (await tenancy.get_workspace(session, workspace_id)).timezone
    rules, exceptions = await catalog.availability_of(session, entity_id)
    now = now or utc_now()
    zone = ZoneInfo(timezone)
    window_start = datetime.combine(first_day, time(), zone)
    window_end = datetime.combine(first_day + timedelta(days=days), time(), zone)
    statement = select(func.lower(Booking.slot), func.upper(Booking.slot)).where(
        Booking.resource_entity_id == entity_id,
        _live(now),
        func.upper(Booking.slot) > window_start,
        func.lower(Booking.slot) < window_end,
    )
    if ignore_booking is not None:  # rescheduling: its own slot doesn't block the move
        statement = statement.where(Booking.id != ignore_booking)
    taken = await session.execute(statement)
    return open_slots(
        rules=[
            WeeklyHours(r.rrule, r.start_time, r.end_time)
            for r in rules
            if r.entity_id == entity_id and r.publication_status == "published"
        ],
        exceptions=[
            DayException(x.exception_date, x.is_available, x.start_time, x.end_time)
            for x in exceptions
            if x.entity_id == entity_id and x.publication_status == "published"
        ],
        timezone=timezone,
        slot_minutes=settings.slot_minutes,
        busy=list(taken.tuples()),
        now=now,
        first_day=first_day,
        days=days,
        notice_minutes=settings.min_notice_minutes,
        horizon_days=settings.horizon_days,
    )


@dataclass(frozen=True)
class BookingView:
    id: uuid.UUID
    resource_entity_id: uuid.UUID
    resource_name: str
    subject_entity_id: uuid.UUID | None
    subject_name: str | None
    starts_at: datetime
    ends_at: datetime
    status: str  # an expired hold reads as "expired"
    hold_until: datetime | None
    source: str
    booked_by: str  # the staff member, or "Voice agent"
    caller_name: str | None
    phone_last4: str | None
    cancel_reason: str | None
    confirmed_at: datetime | None
    created_at: datetime
    row_version: int


async def _views(
    session: AsyncSession,
    vault: Vault,
    rows: list[Booking],
    now: datetime,
    *,
    with_names: bool = True,
) -> list[BookingView]:
    entities = await catalog.entity_names(
        session,
        [b.resource_entity_id for b in rows]
        + [b.subject_entity_id for b in rows if b.subject_entity_id],
    )
    people = await iam.display_names(session, [b.created_by for b in rows if b.created_by])
    views = []
    for b in rows:
        status = (
            "expired" if b.status == "held" and b.hold_until and b.hold_until <= now else b.status
        )
        views.append(
            BookingView(
                b.id,
                b.resource_entity_id,
                entities.get(b.resource_entity_id, "Unknown"),
                b.subject_entity_id,
                entities.get(b.subject_entity_id) if b.subject_entity_id else None,
                b.slot.lower,  # type: ignore[arg-type]  # CHECK: bounded ranges only
                b.slot.upper,  # type: ignore[arg-type]
                status,
                b.hold_until,
                b.source,
                "Voice agent"
                if b.source == "call"
                else people.get(b.created_by, "Staff")
                if b.created_by
                else "Staff",
                vault.open(
                    b.workspace_id,
                    b.id,
                    "subject_name",
                    b.subject_name_ciphertext,
                    b.pii_key_version,
                )
                if with_names
                else None,
                b.phone_last4,
                b.cancel_reason,
                b.confirmed_at,
                b.created_at,
                b.row_version,
            )
        )
    return views


async def bookings_between(
    session: AsyncSession,
    vault: Vault,
    *,
    starts: datetime,
    ends: datetime,
    entity_id: uuid.UUID | None = None,
    include_cancelled: bool = False,
) -> list[BookingView]:
    """Bookings overlapping [starts, ends), earliest first (a fixed number of queries)."""
    statement = select(Booking).where(
        func.upper(Booking.slot) > starts, func.lower(Booking.slot) < ends
    )
    if entity_id is not None:
        statement = statement.where(Booking.resource_entity_id == entity_id)
    if not include_cancelled:
        statement = statement.where(Booking.status.not_in(("cancelled", "expired")))
    rows = list(await session.scalars(statement.order_by(func.lower(Booking.slot), Booking.id)))
    return await _views(session, vault, rows, utc_now())


async def pending_confirmation(session: AsyncSession, vault: Vault) -> list[BookingView]:
    """Held bookings still waiting for staff, soonest first."""
    now = utc_now()
    rows = list(
        await session.scalars(
            select(Booking)
            .where(Booking.status == "held", Booking.hold_until > now)
            .order_by(func.lower(Booking.slot), Booking.id)
            .limit(100)
        )
    )
    return await _views(session, vault, rows, now)


async def get_booking(session: AsyncSession, vault: Vault, booking_id: uuid.UUID) -> BookingView:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise NotFound("Booking not found.")
    [view] = await _views(session, vault, [booking], utc_now(), with_names=False)
    return view
