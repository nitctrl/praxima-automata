"""Slot booking end to end: settings, open slots, staff bookings, holds, privacy, isolation.

Real app, mocked Supabase Auth, real Postgres with forced RLS. Runs only when
PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
import asyncio
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_api_v1_catalog_db import setup
from test_api_v1_db import api, problem, sign_in  # noqa: F401
from test_engagement_pure import make_vault
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401

from praxima.shared.db.engine import Scope, create_engine, scoped_transaction, session_factory

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
IST = ZoneInfo("Asia/Kolkata")


def tomorrow() -> date:
    return datetime.now(IST).date() + timedelta(days=1)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(tomorrow(), time(hour, minute), IST)


def with_hours(browser, headers, base: str) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """A doctor open every day 09:00-10:00 (published) and a service."""
    doctor = browser.post(
        f"{base}/entities",
        json={
            "type": "doctor",
            "key": "dr-asha",
            "name": "Dr Asha",
            "attributes": {"specialization": "ENT"},
        },
        headers=headers,
    ).json()["id"]
    service = browser.post(
        f"{base}/entities",
        json={"type": "service", "key": "consult", "name": "Consultation"},
        headers=headers,
    ).json()["id"]
    rule = browser.post(
        f"{base}/availability-rules",
        json={
            "timezone": "Asia/Kolkata",
            "rrule": "FREQ=DAILY",
            "start_time": "09:00",
            "end_time": "10:00",
            "entity_id": doctor,
        },
        headers=headers,
    ).json()["id"]
    browser.patch(
        f"{base}/availability-rules/{rule}",
        json={"publication_status": "published"},
        headers=headers,
    )
    return doctor, service


def owner_sql(workspace: str, statement: str, **values: object) -> None:
    async def go() -> None:
        engine = create_engine(URL, pooled=False)
        try:
            scope = Scope(workspace_id=uuid.UUID(workspace))
            async with scoped_transaction(session_factory(engine), scope) as session:
                await session.execute(text(statement), values)
        finally:
            await engine.dispose()

    asyncio.run(go())


def held(ws: str, doctor: str, start: datetime, hold_until: datetime) -> str:
    """What the voice agent leaves when staff must confirm (written directly here)."""
    booking_id = str(uuid.uuid4())
    owner_sql(
        ws,
        "INSERT INTO scheduling.bookings (id, workspace_id, resource_entity_id, slot, status,"
        " hold_until, source) VALUES (:id, :ws, :doctor, tstzrange(:start, :end, '[)'),"
        " 'held', :until, 'call')",
        id=booking_id,
        ws=ws,
        doctor=doctor,
        start=start,
        end=start + timedelta(minutes=15),
        until=hold_until,
    )
    return booking_id


def test_settings_slots_and_staff_bookings(api):
    app, _, browser, headers, _, ws = setup(api)
    app.state.vault = make_vault()  # callers' names and numbers are encrypted
    base = f"/api/v1/workspaces/{ws}"
    doctor, service = with_hours(browser, headers, base)

    settings = browser.get(f"{base}/booking-settings").json()
    assert (settings["requires_confirmation"], settings["slot_minutes"]) == (True, 15)
    assert settings["row_version"] == 0
    manager, manager_headers, manager_id = sign_in(app, "manager@one.test")
    browser.put(f"{base}/memberships/{manager_id}", json={"role": "manager"}, headers=headers)
    patch = {"row_version": 0, "min_notice_minutes": 0}
    problem(manager.patch(f"{base}/booking-settings", json=patch, headers=manager_headers), 403)
    saved = browser.patch(f"{base}/booking-settings", json=patch, headers=headers).json()
    assert (saved["min_notice_minutes"], saved["row_version"]) == (0, 1)
    problem(browser.patch(f"{base}/booking-settings", json=patch, headers=headers), 409)

    slots_path = f"{base}/slots?entity_id={doctor}&first_day={tomorrow()}"
    starts = [
        datetime.fromisoformat(s["starts_at"]) for s in browser.get(slots_path).json()["data"]
    ]
    assert starts == [at(9), at(9, 15), at(9, 30), at(9, 45)]
    problem(browser.get(f"{base}/slots?entity_id={service}&first_day={tomorrow()}"), 422)

    booking = {
        "resource_entity_id": doctor,
        "subject_entity_id": service,
        "starts_at": at(9, 15).isoformat(),
        "caller_name": "Asha Rao",
        "phone": "+919812345678",
    }
    key = {"Idempotency-Key": "booking-test-0001"}
    created = browser.post(f"{base}/bookings", json=booking, headers=headers | key)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["status"], body["source"], body["booked_by"]) == (
        "confirmed",
        "staff",
        "owner@one.test",
    )
    assert (body["resource_name"], body["subject_name"], body["phone_last4"]) == (
        "Dr Asha",
        "Consultation",
        "5678",
    )
    assert body["caller_name"] is None  # names only in the audited search or reveal
    again = browser.post(f"{base}/bookings", json=booking, headers=headers | key)
    assert again.json()["id"] == body["id"]  # same key → same booking
    problem(browser.post(f"{base}/bookings", json=booking, headers=headers), 409)  # taken
    outside = booking | {"starts_at": at(11).isoformat()}
    problem(browser.post(f"{base}/bookings", json=outside, headers=headers), 409)
    problem(
        browser.post(f"{base}/bookings", json=booking | {"phone": "98123"}, headers=headers), 422
    )
    assert len(browser.get(slots_path).json()["data"]) == 3

    day = {"starts": at(0).isoformat(), "ends": at(23).isoformat()}
    [listed] = browser.post(f"{base}/bookings/search", json=day, headers=headers).json()["data"]
    assert (listed["caller_name"], listed["starts_at"]) == ("Asha Rao", body["starts_at"])
    revealed = browser.post(f"{base}/bookings/{body['id']}/reveal", headers=headers).json()
    assert revealed == {"caller_name": "Asha Rao", "phone": "+919812345678", "staff_note": None}

    moved = browser.patch(
        f"{base}/bookings/{body['id']}",
        json={"row_version": body["row_version"], "starts_at": at(9).isoformat()},
        headers=headers,
    ).json()
    assert moved["starts_at"] == at(9).isoformat()
    cancelled = browser.post(
        f"{base}/bookings/{body['id']}/cancel",
        json={"row_version": moved["row_version"], "reason": "Patient called to cancel"},
        headers=headers,
    ).json()
    assert (cancelled["status"], cancelled["cancel_reason"]) == (
        "cancelled",
        "Patient called to cancel",
    )
    assert len(browser.get(slots_path).json()["data"]) == 4  # the slot is free again
    assert browser.post(f"{base}/bookings/search", json=day, headers=headers).json()["data"] == []


def test_holds_wait_for_staff_and_expire(api):
    app, _, browser, headers, _, ws = setup(api)
    app.state.vault = make_vault()  # callers' names and numbers are encrypted
    base = f"/api/v1/workspaces/{ws}"
    doctor, _ = with_hours(browser, headers, base)
    browser.patch(
        f"{base}/booking-settings",
        json={"row_version": 0, "min_notice_minutes": 0},
        headers=headers,
    )
    now = datetime.now(IST)
    waiting = held(ws, doctor, at(9, 30), now + timedelta(minutes=30))
    lapsed = held(ws, doctor, at(9, 45), now - timedelta(minutes=1))

    slots_path = f"{base}/slots?entity_id={doctor}&first_day={tomorrow()}"
    starts = [s["starts_at"] for s in browser.get(slots_path).json()["data"]]
    assert starts == [at(9).isoformat(), at(9, 15).isoformat(), at(9, 45).isoformat()]

    [pending] = browser.post(f"{base}/bookings/to-confirm", headers=headers).json()["data"]
    assert (pending["id"], pending["status"], pending["booked_by"]) == (
        waiting,
        "held",
        "Voice agent",
    )
    confirmed = browser.post(
        f"{base}/bookings/{waiting}/confirm", json={"row_version": 1}, headers=headers
    ).json()
    assert (confirmed["status"], confirmed["hold_until"]) == ("confirmed", None)
    problem(
        browser.post(f"{base}/bookings/{lapsed}/confirm", json={"row_version": 1}, headers=headers),
        409,
    )
    # Booking over the lapsed hold works: it is expired first, freeing the slot.
    retake = {
        "resource_entity_id": doctor,
        "starts_at": at(9, 45).isoformat(),
        "caller_name": "Ravi",
    }
    assert browser.post(f"{base}/bookings", json=retake, headers=headers).status_code == 201


def test_roles_isolation_and_the_database_guard(api):
    app, _, browser, headers, _, ws = setup(api)
    app.state.vault = make_vault()  # callers' names and numbers are encrypted
    base = f"/api/v1/workspaces/{ws}"
    doctor, _ = with_hours(browser, headers, base)
    viewer, viewer_headers, viewer_id = sign_in(app, "viewer@one.test")
    browser.put(f"{base}/memberships/{viewer_id}", json={"role": "viewer"}, headers=headers)
    day = {"starts": at(0).isoformat(), "ends": at(23).isoformat()}
    problem(viewer.post(f"{base}/bookings/search", json=day, headers=viewer_headers), 403)
    assert viewer.get(f"{base}/booking-settings").status_code == 200
    _, _, other, other_headers, _, _ = setup(api, "two")
    problem(other.post(f"{base}/bookings/search", json=day, headers=other_headers), 404)
    problem(
        browser.post(
            f"{base}/bookings/search", json=day | {"ends": at(0).isoformat()}, headers=headers
        ),
        422,
    )

    # Even bypassing the API, two live bookings of one doctor can't overlap.
    held(ws, doctor, at(9), datetime.now(IST) + timedelta(hours=1))
    with pytest.raises(IntegrityError):
        held(ws, doctor, at(9, 5), datetime.now(IST) + timedelta(hours=1))
