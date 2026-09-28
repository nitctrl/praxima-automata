"""Agent releases end to end: preview, digest-checked publish, rollback, immutability.

Real app, mocked Supabase Auth, real Postgres with forced RLS. Runs only when
PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from support.queries import count_queries
from test_api_v1_catalog_db import MESSAGES, setup
from test_api_v1_db import api, problem, sign_in  # noqa: F401
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401

from praxima.shared.db.engine import Scope, create_engine, scoped_transaction, session_factory

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")


def publish_all(browser, headers, path: str, row_version: int = 1) -> None:  # type: ignore[no-untyped-def]
    response = browser.patch(
        path, json={"row_version": row_version, "publication_status": "published"}, headers=headers
    )
    assert response.status_code == 200, response.text


def content(browser, headers, base: str) -> dict:  # type: ignore[no-untyped-def, type-arg]
    """One published doctor, one draft doctor, one published FAQ, one live update."""

    def entity(key: str, name: str) -> str:
        created = browser.post(
            f"{base}/entities",
            json={
                "type": "doctor",
                "key": key,
                "name": name,
                "attributes": {"specialization": "ENT"},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    live_doctor = entity("dr-live", "Dr Live")
    draft_doctor = entity("dr-draft", "Dr Draft")
    publish_all(browser, headers, f"{base}/entities/{live_doctor}")
    faq = browser.post(
        f"{base}/faqs",
        json={"canonical_question": "Do you take UPI?", "approved_answer": "Yes, we do."},
        headers=headers,
    ).json()
    publish_all(browser, headers, f"{base}/faqs/{faq['id']}")
    now = datetime.now(timezone.utc)
    update = browser.post(
        f"{base}/announcements",
        json={
            "kind": "closure",
            "public_message": "Closed on Friday for Diwali.",
            "starts_at": (now + timedelta(days=1)).isoformat(),
            "ends_at": (now + timedelta(days=2)).isoformat(),
        },
        headers=headers,
    ).json()
    publish_all(browser, headers, f"{base}/announcements/{update['id']}")
    return {"live": live_doctor, "draft": draft_doctor}


def test_preview_publish_rollback(api):
    app, engine, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    agent = browser.post(
        f"{base}/agents", json={"name": "Desk", "slug": "desk", **MESSAGES}, headers=headers
    ).json()
    doctors = content(browser, headers, base)
    path = f"{base}/agents/{agent['id']}/releases"

    first = browser.post(f"{path}/preview", headers=headers).json()
    assert first["live_version_no"] is None and first["unchanged"] is False
    assert first["summary"]["entities"] == {"doctor": 1}  # drafts never reach callers
    assert first["summary"]["faqs"] == 1 and first["summary"]["announcements"] == 1
    assert first["changes"]["sections"]["entities"] == {"added": 1, "removed": 0, "changed": 0}
    assert any("phone number" in w for w in first["warnings"])
    # Deterministic: the same content always gives the same digest.
    assert browser.post(f"{path}/preview", headers=headers).json()["digest"] == first["digest"]

    stale = "sha256:" + "0" * 64
    problem(browser.post(path, json={"digest": stale}, headers=headers), 409)
    problem(browser.post(path, json={}, headers=headers), 422)
    v1 = browser.post(path, json={"digest": first["digest"]}, headers=headers)
    assert v1.status_code == 201, v1.text
    assert (v1.json()["version_no"], v1.json()["status"]) == (1, "published")
    assert v1.headers["location"].endswith(v1.json()["id"])
    again = browser.post(f"{path}/preview", headers=headers).json()
    assert again["unchanged"] and again["live_version_no"] == 1
    problem(browser.post(path, json={"digest": first["digest"]}, headers=headers), 409)

    # Publishing the draft doctor changes the content: the old preview no longer publishes.
    publish_all(browser, headers, f"{base}/entities/{doctors['draft']}")
    problem(browser.post(path, json={"digest": first["digest"]}, headers=headers), 409)
    second = browser.post(f"{path}/preview", headers=headers).json()
    assert second["changes"]["sections"] == {"entities": {"added": 1, "removed": 0, "changed": 0}}
    v2 = browser.post(path, json={"digest": second["digest"]}, headers=headers).json()
    assert v2["version_no"] == 2 and v2["summary"]["entities"] == {"doctor": 2}

    history = browser.get(path).json()["data"]
    assert [(r["version_no"], r["status"]) for r in history] == [
        (2, "published"),
        (1, "superseded"),
    ]

    rollback = browser.post(path, json={"source_release_id": v1.json()["id"]}, headers=headers)
    assert rollback.status_code == 201, rollback.text
    v3 = rollback.json()
    assert (v3["version_no"], v3["source_release_id"], v3["digest"]) == (
        3,
        v1.json()["id"],
        first["digest"],
    )
    problem(browser.post(path, json={"source_release_id": v1.json()["id"]}, headers=headers), 409)

    detail = browser.get(f"{path}/{v3['id']}").json()
    snapshot = detail["snapshot"]
    assert snapshot["schema_version"] == 4 and snapshot["agent"]["name"] == "Desk"
    assert [e["name"] for e in snapshot["entities"]] == ["Dr Live"]
    assert [f["question"] for f in snapshot["faqs"]] == ["Do you take UPI?"]
    assert {t["key"] for t in snapshot["tools"]} >= {"search_knowledge", "find_entities"}

    with count_queries(engine) as queries:
        browser.get(path)
    assert len(queries) <= 6  # history is one light query plus access checks


def test_roles_isolation_and_immutability(api):
    app, engine, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    agent = browser.post(
        f"{base}/agents", json={"name": "Desk", "slug": "desk", **MESSAGES}, headers=headers
    ).json()
    content(browser, headers, base)
    path = f"{base}/agents/{agent['id']}/releases"
    preview = browser.post(f"{path}/preview", headers=headers).json()

    staff, staff_headers, staff_id = sign_in(app, "staff@one.test")
    browser.put(f"{base}/memberships/{staff_id}", json={"role": "staff"}, headers=headers)
    assert staff.post(f"{path}/preview", headers=staff_headers).status_code == 200
    problem(staff.post(path, json={"digest": preview["digest"]}, headers=staff_headers), 403)

    published = browser.post(path, json={"digest": preview["digest"]}, headers=headers)
    assert published.status_code == 201, published.text
    release = published.json()
    _, _, other, other_headers, _, _ = setup(api, "two")
    problem(other.post(f"{path}/preview", headers=other_headers), 404)
    problem(other.get(f"{path}/{release['id']}"), 404)

    async def tamper() -> None:
        engine = create_engine(URL, pooled=False)
        sessions = session_factory(engine)
        try:
            for statement in (
                "UPDATE releases.agent_releases SET snapshot = '{}'::jsonb",
                "UPDATE releases.agent_releases SET status = 'draft'",
            ):
                with pytest.raises(DBAPIError, match="agent releases are immutable|status change"):
                    async with scoped_transaction(sessions, Scope(workspace_id=ws)) as session:
                        await session.execute(text(statement))
            # No DELETE policy: RLS lets a delete match nothing (the trigger guards the rest).
            async with scoped_transaction(sessions, Scope(workspace_id=ws)) as session:
                deleted = await session.execute(text("DELETE FROM releases.agent_releases"))
                assert deleted.rowcount == 0  # type: ignore[attr-defined]
        finally:
            await engine.dispose()

    asyncio.run(tamper())
    assert browser.get(f"{path}/{release['id']}").json()["release"]["status"] == "published"


def test_voice_runtime_loads_the_live_release_for_the_called_number(api, monkeypatch):
    """The worker's path: trusted called number → agent → live release (one function call)."""
    from praxima.runtime.release.loader import LoadedRelease, NoRelease, load_release

    monkeypatch.setenv("PRAXIMA_RUNTIME_DATABASE_URL", URL)
    app, engine, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    agent = browser.post(
        f"{base}/agents", json={"name": "Desk", "slug": "desk", **MESSAGES}, headers=headers
    ).json()
    content(browser, headers, base)
    number = "+912212345678"
    routed = browser.post(
        f"{base}/agents/{agent['id']}/phone-numbers",
        json={"phone_number": number, "provider": "plivo"},
        headers=headers,
    ).json()

    assert asyncio.run(load_release(number)) == NoRelease("no_live_release")
    path = f"{base}/agents/{agent['id']}/releases"
    digest = browser.post(f"{path}/preview", headers=headers).json()["digest"]
    browser.post(path, json={"digest": digest}, headers=headers)

    loaded = asyncio.run(load_release(number))
    assert isinstance(loaded, LoadedRelease) and loaded.version_no == 1
    assert str(loaded.agent_id) == agent["id"] and str(loaded.workspace_id) == ws
    assert [e.name for e in loaded.snapshot.entities] == ["Dr Live"]
    assert loaded.snapshot.agent.greeting == MESSAGES["greeting_message"]

    assert asyncio.run(load_release("+919000000000")) == NoRelease("unknown_number")
    assert asyncio.run(load_release("98765")) == NoRelease("invalid_number")
    row_version = browser.get(f"{base}/agents/{agent['id']}").json()["row_version"]
    browser.patch(
        f"{base}/agents/{agent['id']}",
        json={"row_version": row_version, "status": "disabled"},
        headers=headers,
    )
    assert asyncio.run(load_release(number)) == NoRelease("agent_disabled")
    browser.delete(f"{base}/agents/{agent['id']}/phone-numbers/{routed['id']}", headers=headers)
    assert asyncio.run(load_release(number)) == NoRelease("unknown_number")

    monkeypatch.setenv("PRAXIMA_RUNTIME_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/x")
    assert asyncio.run(load_release(number)) == NoRelease("database_unavailable")
