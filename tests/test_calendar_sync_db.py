"""Google Calendar sync end to end (migration 0014), against a fake Google.

Connect through the OAuth callback, then the worker puts confirmed bookings in the calendar,
removes cancelled ones, and reads busy time that blocks slots. Runs only when
PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from test_api_v1_catalog_db import setup
from test_api_v1_db import api, client, problem, sign_in  # noqa: F401
from test_api_v1_scheduling_db import at, tomorrow, with_hours
from test_engagement_pure import make_vault
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401

from praxima.entrypoints.jobs import run_once
from praxima.integrations.google.calendar import GoogleCalendar, GoogleConfig
from praxima.shared.db.engine import create_engine, session_factory

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
CONFIG = GoogleConfig(
    "client-1", "secret-1", "http://127.0.0.1:3000/api/v1/integrations/google/callback"
)
REFRESH = "refresh-token-SECRET"


class FakeGoogle:
    """Records what Google was asked; `revoked` makes token refresh fail like Google does."""

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}  # type: ignore[type-arg]
        self.deleted: list[str] = []
        self.revoked = False
        self.revocations = 0
        self.busy: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "a1", "refresh_token": REFRESH})
            if self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert form["refresh_token"] == [REFRESH]
            return httpx.Response(200, json={"access_token": "a2"})
        if path == "/v1/userinfo":
            return httpx.Response(200, json={"email": "dr.asha@gmail.com"})
        if path == "/revoke":
            self.revocations += 1
            return httpx.Response(200)
        if path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"primary": {"busy": self.busy}}})
        if path.endswith("/events") and request.method == "POST":
            event_id = f"ev{len(self.events) + 1}"
            self.events[event_id] = json.loads(request.content)
            return httpx.Response(200, json={"id": event_id})
        event_id = path.rsplit("/", 1)[1]
        if request.method == "PATCH":
            self.events[event_id] = json.loads(request.content)
            return httpx.Response(200, json={"id": event_id})
        if request.method == "DELETE":
            self.deleted.append(event_id)
            self.events.pop(event_id, None)
            return httpx.Response(204)
        return httpx.Response(500)


def work(engine_url: str, vault, google: GoogleCalendar) -> None:  # type: ignore[no-untyped-def]
    """Run the background worker until no job is left."""

    async def go() -> None:
        engine = create_engine(engine_url, pooled=False)
        try:
            for _ in range(10):
                if not await run_once(session_factory(engine), vault, google):
                    return
        finally:
            await engine.dispose()

    asyncio.run(go())


def outbox(ws: str, status: str) -> list[str]:
    """Job kinds with a status, read inside the workspace's scope (forced RLS)."""
    engine = create_sync_engine(URL)
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": ws})
        kinds = (
            connection.execute(
                text("SELECT kind FROM ops.outbox WHERE status = :s ORDER BY created_at"),
                {"s": status},
            )
            .scalars()
            .all()
        )
    engine.dispose()
    return list(kinds)


def test_connect_sync_bookings_busy_time_and_disconnect(api):
    fake = FakeGoogle()
    google = GoogleCalendar(CONFIG, httpx.MockTransport(fake))
    app, _, browser, headers, _, ws = setup(api)
    vault = make_vault()
    app.state.vault, app.state.google = vault, google
    base = f"/api/v1/workspaces/{ws}"
    doctor, _ = with_hours(browser, headers, base)
    browser.patch(
        f"{base}/booking-settings",
        json={"row_version": 0, "min_notice_minutes": 0},
        headers=headers,
    )
    connection = f"{base}/entities/{doctor}/calendar-connection"

    assert browser.get(connection).json() == {
        "configured": True,
        "status": "none",
        "account_email": None,
        "error_code": None,
        "last_synced_at": None,
    }
    staff, staff_headers, staff_id = sign_in(app, "staff@one.test")
    browser.put(f"{base}/memberships/{staff_id}", json={"role": "staff"}, headers=headers)
    problem(staff.post(connection, headers=staff_headers), 403)  # managers connect calendars

    url = browser.post(connection, headers=headers).json()["authorize_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    # Google sends the browser back without our session cookie: the signed state is enough.
    back = client(app).get(
        "/api/v1/integrations/google/callback",
        params={"state": state, "code": "auth-code"},
        follow_redirects=False,
    )
    assert back.status_code == 303
    assert back.headers["location"].endswith(f"/directory?entity={doctor}&calendar=connected")
    status = browser.get(connection).json()
    assert (status["status"], status["account_email"]) == ("active", "dr.asha@gmail.com")
    engine = create_sync_engine(URL)
    with engine.begin() as db:  # forced RLS: read it inside the workspace's scope
        db.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": ws})
        stored = db.execute(
            text("SELECT refresh_token_ciphertext FROM scheduling.calendar_connections")
        ).scalar_one()
    engine.dispose()
    assert REFRESH.encode() not in stored  # encrypted at rest
    tampered = client(app).get(
        "/api/v1/integrations/google/callback",
        params={"state": state[:-2] + "xx", "code": "c"},
        follow_redirects=False,
    )
    assert tampered.headers["location"].endswith("/schedule?calendar=expired")

    # Busy time in Google (09:30-09:45 tomorrow) blocks that slot after the first sync.
    fake.busy = [{"start": at(9, 30).isoformat(), "end": at(9, 45).isoformat()}]
    booking = {
        "resource_entity_id": doctor,
        "starts_at": at(9).isoformat(),
        "caller_name": "Asha Rao",
        "phone": "+919812345678",
    }
    booked = browser.post(f"{base}/bookings", json=booking, headers=headers).json()
    work(URL, vault, google)
    slots = browser.get(f"{base}/slots?entity_id={doctor}&first_day={tomorrow()}").json()["data"]
    assert [s["starts_at"] for s in slots] == [at(9, 15).isoformat(), at(9, 45).isoformat()]
    [event] = fake.events.values()
    assert event["summary"] == "Appointment: Asha Rao"
    assert "+919812345678" in event["description"] and "Booked by staff" in event["description"]
    assert event["start"] == {"dateTime": at(9).isoformat(), "timeZone": "Asia/Kolkata"}
    detail = browser.get(f"{base}/bookings/{booked['id']}").json()
    assert detail["calendar_sync_status"] == "synced"
    assert browser.get(connection).json()["last_synced_at"] is not None

    # Moving the booking moves the event; cancelling removes it.
    moved = browser.patch(
        f"{base}/bookings/{booked['id']}",
        json={"row_version": detail["row_version"], "starts_at": at(9, 15).isoformat()},
        headers=headers,
    ).json()
    work(URL, vault, google)
    assert list(fake.events.values())[0]["start"]["dateTime"] == at(9, 15).isoformat()
    browser.post(
        f"{base}/bookings/{booked['id']}/cancel",
        json={"row_version": moved["row_version"]},
        headers=headers,
    )
    work(URL, vault, google)
    assert fake.deleted and not fake.events
    assert (
        browser.get(f"{base}/bookings/{booked['id']}").json()["calendar_sync_status"] == "removed"
    )

    # Access revoked in Google: the connection shows the error and jobs stop retrying.
    fake.revoked = True
    browser.post(
        f"{base}/bookings", json=booking | {"starts_at": at(9).isoformat()}, headers=headers
    )
    work(URL, vault, google)
    status = browser.get(connection).json()
    assert (status["status"], status["error_code"]) == ("error", "revoked")
    assert "calendar.sync_booking" in outbox(ws, "failed")

    assert browser.delete(connection, headers=headers).status_code == 204
    assert fake.revocations == 1 and browser.get(connection).json()["status"] == "none"
    slots = browser.get(f"{base}/slots?entity_id={doctor}&first_day={tomorrow()}").json()["data"]
    assert at(9, 30).isoformat() in [s["starts_at"] for s in slots]  # Google busy time forgotten


def test_without_google_configured(api):
    app, _, browser, headers, _, ws = setup(api)
    app.state.vault = make_vault()
    base = f"/api/v1/workspaces/{ws}"
    doctor, _ = with_hours(browser, headers, base)
    connection = f"{base}/entities/{doctor}/calendar-connection"
    assert browser.get(connection).json()["configured"] is False
    problem(browser.post(connection, headers=headers), 503)


def test_only_the_owner_run_worker_sees_jobs_across_workspaces(admin_id):
    engine = create_sync_engine(URL)
    with engine.begin() as db:
        ws = db.execute(text("SELECT gen_random_uuid()")).scalar_one()
        db.execute(text("SELECT set_config('app.workspace_id', :ws, true)"), {"ws": str(ws)})
        # In some other workspace's scope (or none), jobs of other workspaces are invisible.
        assert db.execute(text("SELECT ops.is_worker()")).scalar_one() is False
        db.execute(text("SELECT set_config('app.worker', 'on', true)"))
        # The flag alone counts only together with being the table owner (this test login is).
        owner = db.execute(
            text(
                "SELECT pg_get_userbyid(relowner) = current_user FROM pg_class"
                " WHERE oid = 'ops.outbox'::regclass"
            )
        ).scalar_one()
        assert db.execute(text("SELECT ops.is_worker()")).scalar_one() is owner
    engine.dispose()
