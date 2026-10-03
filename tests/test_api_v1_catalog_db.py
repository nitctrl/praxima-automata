# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
"""/api/v1 catalog and agents end to end: real app, mocked Supabase Auth, real Postgres.

Runs only when PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

import httpx
import pytest
from support.queries import count_queries
from test_api_v1_db import ORIGIN, api, onboard, problem, sign_in  # noqa: F401
from test_iam_tenancy_db import CLINIC, URL, admin_id, migrated  # noqa: F401

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")

WORKSPACE = {
    "slug": "branch-2",
    "name": "Second branch",
    "pack_key": "clinic",
    "pack_version": CLINIC.version,
    "timezone": "Asia/Kolkata",
    "default_language": "hi-IN",
    "supported_languages": ["hi-IN", "en-IN"],
}
MESSAGES = {
    "greeting_message": "Namaste, how can I help?",
    "emergency_message": "Please call 112 now.",
    "fallback_message": "Our staff will call you back.",
}


def setup(api, slug: str = "one"):  # type: ignore[no-untyped-def]
    """Owner signs in, a platform admin onboards the org, the owner creates a workspace."""
    app, engine, admin = api
    browser, headers, owner = sign_in(app, f"owner@{slug}.test")
    org, _ = onboard(engine, admin, slug, owner)
    created = browser.post(
        f"/api/v1/organizations/{org}/workspaces", json=WORKSPACE, headers=headers
    )
    assert created.status_code == 201, created.text
    assert created.headers["location"] == f"/api/v1/workspaces/{created.json()['id']}"
    return app, engine, browser, headers, org, created.json()["id"]


def test_workspace_pack_vocabulary(api):
    app, _, browser, _, _, ws = setup(api)
    pack = browser.get(f"/api/v1/workspaces/{ws}/pack").json()
    assert (pack["key"], pack["version"], pack["callback_kind"]) == (
        "clinic",
        CLINIC.version,
        "callback_request",
    )
    assert pack["entity_labels"]["doctor"] == {"name": "Doctor", "plural_name": "Doctors"}
    assert "about" in pack["document_categories"] and "closure" in pack["announcement_kinds"]
    assert "112" in pack["agent_defaults"]["emergency_message"]
    _, _, other, _, _, _ = setup(api, "two")
    problem(other.get(f"/api/v1/workspaces/{ws}/pack"), 404)


def test_workspace_creation_installs_the_pack(api):
    app, _, browser, _, _, ws = setup(api)
    workspace = browser.get(f"/api/v1/workspaces/{ws}").json()
    assert workspace["industry"] == "healthcare"  # taken from the pack
    types = browser.get(f"/api/v1/workspaces/{ws}/entity-types").json()["data"]
    assert [t["key"] for t in types] == ["doctor", "location", "service"]
    assert browser.get("/api/v1/packs").json()["data"] == [
        {"key": "clinic", "version": CLINIC.version, "name": "Clinic", "industry": "healthcare"}
    ]


def test_entities_lifecycle(api):
    app, engine, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"

    def create(body: dict) -> httpx.Response:  # type: ignore[type-arg]
        return browser.post(f"{base}/entities", json=body, headers=headers)

    doctor = create(
        {
            "type": "doctor",
            "key": "dr-sharma",
            "name": "Dr. Asha Sharma",
            "aliases": ["Sharma ji"],
            "attributes": {"specialization": "Cardiology", "experience_years": 15},
        }
    )
    assert doctor.status_code == 201 and doctor.headers["location"].endswith(doctor.json()["id"])
    entity = doctor.json()
    assert (entity["type"], entity["publication_status"], entity["row_version"]) == (
        "doctor",
        "draft",
        1,
    )

    bad = create({"type": "doctor", "key": "dr-x", "name": "X", "attributes": {"ssn": "123"}})
    fields = {e["field"] for e in problem(bad, 422)["errors"]}
    assert fields == {"attributes.ssn", "attributes.specialization"} and "123" not in bad.text
    problem(create({"type": "doctor", "key": "Bad Key", "name": "X"}), 422)
    unknown_field = {"type": "service", "key": "s-1", "name": "S", "colour": "red"}
    assert [e["field"] for e in problem(create(unknown_field), 422)["errors"]] == ["colour"]
    duplicate_key = {"type": "doctor", "key": "dr-sharma", "name": "Dup"}
    problem(create(duplicate_key | {"attributes": {"specialization": "ENT"}}), 409)

    path = f"{base}/entities/{entity['id']}"
    published = browser.patch(
        path,
        json={"row_version": 1, "name": "Dr. A. Sharma", "publication_status": "published"},
        headers=headers,
    ).json()
    assert (published["name"], published["publication_status"], published["row_version"]) == (
        "Dr. A. Sharma",
        "published",
        2,
    )
    problem(browser.patch(path, json={"row_version": 1, "name": "Late"}, headers=headers), 409)
    found = browser.get(f"{base}/entities", params={"q": "sharma JI"}).json()["data"]
    assert [e["id"] for e in found] == [entity["id"]]
    assert browser.get(f"{base}/entities", params={"status": "draft"}).json()["data"] == []

    problem(browser.delete(path, headers=headers), 422)  # row_version required
    problem(browser.delete(path, params={"row_version": 1}, headers=headers), 409)
    assert browser.delete(path, params={"row_version": 2}, headers=headers).status_code == 204
    problem(browser.get(path), 404)


def test_entity_list_cost_is_flat_and_workspaces_are_isolated(api):
    app, engine, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"

    def add(n: int) -> str:
        body = {"type": "service", "key": f"s-{n}", "name": f"Service {n}"}
        return browser.post(f"{base}/entities", json=body, headers=headers).json()["id"]

    first = add(0)
    with count_queries(engine) as one:
        assert len(browser.get(f"{base}/entities").json()["data"]) == 1
    for n in range(1, 5):
        add(n)
    with count_queries(engine) as five:
        assert len(browser.get(f"{base}/entities").json()["data"]) == 5
    assert one and len(five) == len(one)

    other_app, _, other, other_headers, _, other_ws = setup(api, "two")
    problem(other.get(f"{base}/entities/{first}"), 404)  # not their workspace
    problem(other.get(f"/api/v1/workspaces/{other_ws}/entities/{first}"), 404)  # not in theirs
    body = {"type": "service", "key": "steal", "name": "X"}
    problem(other.post(f"{base}/entities", json=body, headers=other_headers), 404)


def test_relations_and_hours(api):
    app, _, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    doctor = browser.post(
        f"{base}/entities",
        json={
            "type": "doctor",
            "key": "dr-a",
            "name": "Dr A",
            "attributes": {"specialization": "ENT"},
        },
        headers=headers,
    ).json()["id"]
    service = browser.post(
        f"{base}/entities",
        json={"type": "service", "key": "consult", "name": "Consult"},
        headers=headers,
    ).json()["id"]

    kinds = browser.get(f"{base}/relation-types").json()["data"]
    assert [(k["key"], k["from_type"], k["to_type"]) for k in kinds] == [
        ("doctor_offers_service", "doctor", "service"),
        ("doctor_at_location", "doctor", "location"),
    ]
    assert set(kinds[0]["attributes_schema"]["properties"]) == {"fee", "currency"}

    link = {
        "relation_type": "doctor_offers_service",
        "from_entity_id": doctor,
        "to_entity_id": service,
        "attributes": {"fee": 800, "currency": "INR"},
    }
    created = browser.post(f"{base}/relations", json=link, headers=headers)
    assert created.status_code == 201
    problem(browser.post(f"{base}/relations", json=link, headers=headers), 409)
    reversed_link = link | {"from_entity_id": service, "to_entity_id": doctor}
    problem(browser.post(f"{base}/relations", json=reversed_link, headers=headers), 422)
    relation_id = created.json()["id"]
    assert (
        browser.patch(
            f"{base}/relations/{relation_id}",
            json={"publication_status": "published"},
            headers=headers,
        ).status_code
        == 204
    )
    relations = browser.get(f"{base}/entities/{doctor}/relations").json()["data"]
    assert [
        (r["other_name"], r["attributes"]["fee"], r["publication_status"]) for r in relations
    ] == [("Consult", 800, "published")]

    rule = {
        "timezone": "Asia/Kolkata",
        "rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
        "start_time": "09:00",
        "end_time": "13:00",
        "entity_id": doctor,
    }
    assert browser.post(f"{base}/availability-rules", json=rule, headers=headers).status_code == 201
    problem(
        browser.post(f"{base}/availability-rules", json=rule | {"rrule": "daily"}, headers=headers),
        422,
    )
    problem(
        browser.post(
            f"{base}/availability-rules", json=rule | {"entity_id": service}, headers=headers
        ),
        422,
    )
    closed = {
        "timezone": "Asia/Kolkata",
        "exception_date": "2026-10-20",
        "is_available": False,
        "entity_id": doctor,
        "public_message": "On leave for Diwali.",
    }
    assert (
        browser.post(f"{base}/availability-exceptions", json=closed, headers=headers).status_code
        == 201
    )
    hours = browser.get(f"{base}/entities/{doctor}/availability").json()
    assert [(r["rrule"], r["start_time"]) for r in hours["rules"]] == [
        ("FREQ=WEEKLY;BYDAY=MO,WE,FR", "09:00:00")
    ]
    assert [e["exception_date"] for e in hours["exceptions"]] == ["2026-10-20"]


def test_agents_tools_numbers_and_roles(api):
    app, engine, browser, headers, org, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}/agents"
    created = browser.post(
        base, json={"name": "Reception", "slug": "reception", **MESSAGES}, headers=headers
    )
    assert created.status_code == 201
    agent = created.json()
    assert len(agent["tools"]) == len(CLINIC.tools) and all(t["enabled"] for t in agent["tools"])
    path = f"{base}/{agent['id']}"

    off = browser.put(f"{path}/tools/request_callback", json={"enabled": False}, headers=headers)
    assert off.json() == {"key": "request_callback", "enabled": False}
    problem(
        browser.put(f"{path}/tools/book_appointment", json={"enabled": True}, headers=headers), 422
    )

    number = browser.post(
        f"{path}/phone-numbers",
        json={"phone_number": "+919876543210", "provider": "plivo"},
        headers=headers,
    )
    assert number.status_code == 201 and number.json()["status"] == "active"
    problem(
        browser.post(
            f"{path}/phone-numbers",
            json={"phone_number": "98765", "provider": "plivo"},
            headers=headers,
        ),
        422,
    )
    renamed = browser.patch(
        path, json={"row_version": 1, "name": "Front desk"}, headers=headers
    ).json()
    assert renamed["name"] == "Front desk" and renamed["row_version"] == 2
    problem(
        browser.patch(path, json={"row_version": 1, "status": "disabled"}, headers=headers), 409
    )

    # A second tenant can't take the same number while it's active.
    _, _, other, other_headers, _, other_ws = setup(api, "two")
    other_agent = other.post(
        f"/api/v1/workspaces/{other_ws}/agents",
        json={"name": "Desk", "slug": "desk", **MESSAGES},
        headers=other_headers,
    ).json()["id"]
    taken = other.post(
        f"/api/v1/workspaces/{other_ws}/agents/{other_agent}/phone-numbers",
        json={"phone_number": "+919876543210", "provider": "plivo"},
        headers=other_headers,
    )
    assert problem(taken, 409)["detail"] == "This number is already in use."

    # Roles: a viewer reads but can't change; managers can't route phone numbers.
    viewer, viewer_headers, viewer_id = sign_in(app, "viewer@one.test")
    manager, manager_headers, manager_id = sign_in(app, "manager@one.test")
    for user_id, role in ((viewer_id, "viewer"), (manager_id, "manager")):
        browser.put(
            f"/api/v1/workspaces/{ws}/memberships/{user_id}", json={"role": role}, headers=headers
        )
    assert viewer.get(path).status_code == 200
    problem(viewer.patch(path, json={"row_version": 2, "name": "x"}, headers=viewer_headers), 403)
    problem(
        manager.post(
            f"{path}/phone-numbers",
            json={"phone_number": "+919800000000", "provider": "plivo"},
            headers=manager_headers,
        ),
        403,
    )
    released = browser.delete(f"{path}/phone-numbers/{number.json()['id']}", headers=headers)
    assert released.status_code == 204
    assert browser.get(path).json()["phone_numbers"] == []
