"""Scheduling endpoints. No business logic or queries here."""

import uuid
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from praxima.entrypoints.http.deps import PiiVault, WorkspaceAccess
from praxima.entrypoints.http.responses import Page, PageInfo
from praxima.modules import iam, scheduling
from praxima.modules.scheduling.api.schemas import (
    BookingIn,
    BookingOut,
    BookingSearchIn,
    BookingSettingsOut,
    BookingSettingsPatch,
    CancelIn,
    RescheduleIn,
    RevealedBookingOut,
    RowVersionIn,
    SlotOut,
)
from praxima.shared.errors import FieldError, ValidationFailed

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["scheduling"])
IdempotencyKey = Annotated[
    str | None,
    Header(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Send the same key when retrying; the first booking is returned.",
    ),
]


def _ws(access: WorkspaceAccess) -> uuid.UUID:
    assert access.workspace_id is not None
    return access.workspace_id


def _page(items: list[BookingOut]) -> Page[BookingOut]:
    return Page(data=items, page=PageInfo(limit=len(items), next_cursor=None))


@router.get("/booking-settings")
async def read_booking_settings(access: WorkspaceAccess) -> BookingSettingsOut:
    iam.require(access.actor, "workspace:read")
    return BookingSettingsOut.of(await scheduling.booking_settings(access.session, _ws(access)))


@router.patch("/booking-settings")
async def update_booking_settings(
    body: BookingSettingsPatch, access: WorkspaceAccess
) -> BookingSettingsOut:
    """Admins: whether staff confirm the agent's bookings, hold time, notice, window."""
    try:
        changes = body.changes()
    except ValueError:
        raise ValidationFailed(
            errors=[FieldError("slot_minutes", "Use 0 for the pack's length, or 5 to 480.")]
        ) from None
    await scheduling.update_settings(
        access.session,
        access.actor,
        workspace_id=_ws(access),
        row_version=body.row_version,
        changes=changes,
    )
    return BookingSettingsOut.of(await scheduling.booking_settings(access.session, _ws(access)))


@router.get("/slots")
async def list_open_slots(
    access: WorkspaceAccess,
    entity_id: uuid.UUID,
    first_day: date,
    days: Annotated[int, Query(ge=1, le=14)] = 1,
) -> Page[SlotOut]:
    """Open slots of one bookable entry: inside published hours, not booked."""
    iam.require(access.actor, "bookings:read")
    settings = await scheduling.booking_settings(access.session, _ws(access))
    starts = await scheduling.open_slots_for(
        access.session, _ws(access), entity_id, first_day=first_day, days=days
    )
    length = timedelta(minutes=settings.slot_minutes)
    slots = [SlotOut(starts_at=s, ends_at=s + length) for s in starts]
    return Page(data=slots, page=PageInfo(limit=len(slots), next_cursor=None))


@router.post("/bookings/search")
async def search_bookings(
    body: BookingSearchIn, access: WorkspaceAccess, vault: PiiVault
) -> Page[BookingOut]:
    """The schedule with callers' names (a POST: viewing names is audited)."""
    views = await scheduling.list_bookings(
        access.session,
        access.actor,
        vault,
        workspace_id=_ws(access),
        starts=body.starts,
        ends=body.ends,
        entity_id=body.entity_id,
        include_cancelled=body.include_cancelled,
    )
    return _page([BookingOut.of(v) for v in views])


@router.post("/bookings/to-confirm")
async def bookings_to_confirm(access: WorkspaceAccess, vault: PiiVault) -> Page[BookingOut]:
    """Holds the agent made that wait for staff (with names; audited)."""
    views = await scheduling.to_confirm(
        access.session, access.actor, vault, workspace_id=_ws(access)
    )
    return _page([BookingOut.of(v) for v in views])


@router.post("/bookings", status_code=status.HTTP_201_CREATED)
async def create_booking(
    body: BookingIn,
    access: WorkspaceAccess,
    vault: PiiVault,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> BookingOut:
    """Staff book an open slot for someone; it is confirmed at once."""
    booking_id = await scheduling.book(
        access.session,
        access.actor,
        vault,
        workspace_id=_ws(access),
        draft=body.draft(idempotency_key),
    )
    response.headers["Location"] = f"/api/v1/workspaces/{_ws(access)}/bookings/{booking_id}"
    return BookingOut.of(
        await scheduling.booking_detail(access.session, access.actor, vault, booking_id)
    )


@router.get("/bookings/{booking_id}")
async def read_booking(
    booking_id: uuid.UUID, access: WorkspaceAccess, vault: PiiVault
) -> BookingOut:
    return BookingOut.of(
        await scheduling.booking_detail(access.session, access.actor, vault, booking_id)
    )


@router.post("/bookings/{booking_id}/confirm")
async def confirm_booking(
    booking_id: uuid.UUID, body: RowVersionIn, access: WorkspaceAccess, vault: PiiVault
) -> BookingOut:
    await scheduling.confirm(
        access.session, access.actor, booking_id=booking_id, row_version=body.row_version
    )
    return BookingOut.of(
        await scheduling.booking_detail(access.session, access.actor, vault, booking_id)
    )


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(
    booking_id: uuid.UUID, body: CancelIn, access: WorkspaceAccess, vault: PiiVault
) -> BookingOut:
    await scheduling.cancel(
        access.session,
        access.actor,
        booking_id=booking_id,
        row_version=body.row_version,
        reason=body.reason,
    )
    return BookingOut.of(
        await scheduling.booking_detail(access.session, access.actor, vault, booking_id)
    )


@router.patch("/bookings/{booking_id}")
async def reschedule_booking(
    booking_id: uuid.UUID, body: RescheduleIn, access: WorkspaceAccess, vault: PiiVault
) -> BookingOut:
    """Move an upcoming booking to another open slot of the same entry."""
    await scheduling.reschedule(
        access.session,
        access.actor,
        booking_id=booking_id,
        row_version=body.row_version,
        starts_at=body.starts_at,
    )
    return BookingOut.of(
        await scheduling.booking_detail(access.session, access.actor, vault, booking_id)
    )


@router.post("/bookings/{booking_id}/reveal")
async def reveal_booking(
    booking_id: uuid.UUID, access: WorkspaceAccess, vault: PiiVault
) -> RevealedBookingOut:
    """Name, full phone and staff note (staff+). Every call is audited."""
    revealed = await scheduling.reveal(access.session, access.actor, vault, booking_id=booking_id)
    return RevealedBookingOut(**revealed.__dict__)
