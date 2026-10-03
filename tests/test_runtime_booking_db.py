"""Booking on a call: the voice agent offers open slots and books one (migration 0013).

Real app and voice runtime code, mocked Supabase Auth, real Postgres with forced RLS. Runs only
when PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
import asyncio
import base64
import json
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from test_api_v1_catalog_db import MESSAGES, setup
from test_api_v1_db import api, problem, sign_in  # noqa: F401
from test_api_v1_scheduling_db import at, tomorrow, with_hours
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401

from praxima.modules.engagement import Vault
from praxima.shared.db.engine import Scope, create_engine, scoped_transaction, session_factory
from praxima.shared.security.lookup import PhoneLookup
from praxima.shared.security.privacy import PiiCipher

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
NUMBER, CALLER = "+912212345670", "+919812345678"


def live_agent(api, monkeypatch):  # type: ignore[no-untyped-def]
    """A workspace whose published release has a bookable doctor open 09:00-10:00 daily."""
    key = os.urandom(32)
    monkeypatch.setenv("PRAXIMA_RUNTIME_DATABASE_URL", URL)
    monkeypatch.setenv("CLINIC_PII_KEYS", json.dumps({"v1": base64.b64encode(key).decode()}))
    monkeypatch.setenv("CLINIC_PII_KEY_VERSION", "v1")
    app, _, browser, headers, _, ws = setup(api)
    app.state.vault = Vault(PiiCipher({"v1": key}, "v1"), PhoneLookup(os.urandom(32)))
    base = f"/api/v1/workspaces/{ws}"
    doctor, service = with_hours(browser, headers, base)
    for entity in (doctor, service):
        browser.patch(
            f"{base}/entities/{entity}",
            json={"row_version": 1, "publication_status": "published"},
            headers=headers,
        )
    agent = browser.post(
        f"{base}/agents", json={"name": "Desk", "slug": "desk", **MESSAGES}, headers=headers
    ).json()
    browser.post(
        f"{base}/agents/{agent['id']}/phone-numbers",
        json={"phone_number": NUMBER, "provider": "plivo"},
        headers=headers,
    )
    path = f"{base}/agents/{agent['id']}/releases"
    digest = browser.post(f"{path}/preview", headers=headers).json()["digest"]
    assert browser.post(path, json={"digest": digest}, headers=headers).status_code == 201
    return browser, headers, base, ws, doctor


def test_agent_offers_and_books_open_slots(api, monkeypatch):
    from praxima.runtime.release.knowledge import ReleaseKnowledge
    from praxima.runtime.release.loader import load_release

    browser, headers, base, _, doctor = live_agent(api, monkeypatch)
    day = tomorrow().isoformat()
    sip = {"sip.callID": "SCL_booking1", "sip.phoneNumber": CALLER}

    async def call() -> dict:  # type: ignore[type-arg]
        knowledge = ReleaseKnowledge(await load_release(NUMBER))
        names = {t.info.name for t in knowledge.function_tools()}
        assert {"find_open_slots", "book_slot"} <= names
        await knowledge.start_call(called_number=NUMBER, is_sip=True, attributes=sip)
        results = {
            "find": await knowledge.find_open_slots("Dr Asha", day),
            "no_number": await knowledge.book_slot("Dr Asha", day, "09:15", "Ravi Kumar"),
            "closed": await knowledge.book_slot(
                "Dr Asha", day, "11:00", "Ravi Kumar", use_calling_number=True
            ),
            "held": await knowledge.book_slot(
                "Dr Asha", day, "09:15", "Ravi Kumar", use_calling_number=True, about="Consultation"
            ),
            "again": await knowledge.book_slot(
                "Dr Asha", day, "09:15", "Ravi Kumar", use_calling_number=True
            ),
            "after": await knowledge.find_open_slots("Dr Asha", day),
            "service": await knowledge.find_open_slots("Consultation", day),
        }
        await knowledge.finish_call()
        return results

    results = asyncio.run(call())
    assert [s["time"] for s in results["find"]["slots"]] == ["09:00", "09:15", "09:30", "09:45"]
    assert results["find"]["requires_staff_confirmation"] is True
    assert results["no_number"]["status"] == "invalid"
    assert "callback_number" in results["no_number"]["fix"]
    assert results["closed"]["status"] == "not_open"
    assert [s["time"] for s in results["closed"]["alternatives"]] == [
        "09:00",
        "09:15",
        "09:30",
        "09:45",
    ]
    assert results["held"]["status"] == "held" and "confirm" in results["held"]["say"]
    assert results["again"]["status"] == "held"  # a retried tool call doesn't book twice
    assert [s["time"] for s in results["after"]["slots"]] == ["09:00", "09:30", "09:45"]
    assert results["service"]["status"] == "not_bookable"

    # Staff see the hold with the caller's name, booked by the voice agent.
    [held] = browser.post(f"{base}/bookings/to-confirm", headers=headers).json()["data"]
    assert (held["caller_name"], held["booked_by"], held["source"]) == (
        "Ravi Kumar",
        "Voice agent",
        "call",
    )
    assert (held["phone_last4"], held["subject_name"]) == (CALLER[-4:], "Consultation")
    revealed = browser.post(f"{base}/bookings/{held['id']}/reveal", headers=headers).json()
    assert revealed["phone"] == CALLER
    [record] = browser.get(f"{base}/conversations").json()["data"]
    assert (record["primary_intent"], record["disposition"]) == ("booking", "request_created")
    events = browser.get(f"{base}/conversations/{record['id']}").json()["events"]
    assert {"kind": "booking", "status": "held"} in [e["sanitized_payload"] for e in events]
    assert "Ravi" not in json.dumps(events) and CALLER not in json.dumps(events)


def test_instant_booking_when_staff_confirmation_is_off(api, monkeypatch):
    from praxima.runtime.release.knowledge import ReleaseKnowledge
    from praxima.runtime.release.loader import load_release

    browser, headers, base, _, _ = live_agent(api, monkeypatch)
    settings = browser.patch(
        f"{base}/booking-settings",
        json={"row_version": 0, "requires_confirmation": False},
        headers=headers,
    )
    assert settings.status_code == 200
    day = tomorrow().isoformat()

    async def call() -> dict:  # type: ignore[type-arg]
        knowledge = ReleaseKnowledge(await load_release(NUMBER))
        await knowledge.start_call(called_number=NUMBER, is_sip=False, attributes={})
        booked = await knowledge.book_slot(
            "Dr Asha", day, "09:30", "Meera", callback_number="+91 98765 43210"
        )
        await knowledge.finish_call()
        return booked

    booked = asyncio.run(call())
    assert booked["status"] == "confirmed" and "booked" in booked["say"]
    window = {"starts": at(0).isoformat(), "ends": at(23).isoformat()}
    [listed] = browser.post(f"{base}/bookings/search", json=window, headers=headers).json()["data"]
    assert (listed["status"], listed["caller_name"], listed["phone_last4"]) == (
        "confirmed",
        "Meera",
        "3210",
    )


def test_runtime_function_refuses_overlaps_and_bad_input(api, monkeypatch):
    browser, headers, base, ws, doctor = live_agent(api, monkeypatch)

    async def book(key: str, starts, entity: str = doctor) -> dict:  # type: ignore[no-untyped-def,type-arg]
        engine = create_engine(URL, pooled=False)
        try:
            async with scoped_transaction(session_factory(engine), Scope()) as session:
                return (
                    await session.execute(
                        text(
                            "SELECT scheduling.runtime_book_slot(:ws, NULL, NULL, :id, :entity,"
                            " NULL, :starts, :key, NULL, NULL, NULL, NULL)"
                        ),
                        {
                            "ws": ws,
                            "id": str(uuid.uuid4()),
                            "entity": entity,
                            "starts": starts,
                            "key": key,
                        },
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

    first = asyncio.run(book("k1", at(9)))
    assert first["status"] == "held" and first["created"] is True
    assert asyncio.run(book("k1", at(9)))["created"] is False  # idempotent
    assert asyncio.run(book("k2", at(9, 5)))["status"] == "taken"  # overlaps 09:00-09:15
    assert asyncio.run(book("k3", at(9) - timedelta(days=60)))["status"] == "outside_window"
