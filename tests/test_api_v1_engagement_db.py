"""/api/v1 CRM end to end: real app, mocked Supabase Auth, real Postgres.

Runs only when PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
import pytest
from support.queries import count_queries
from test_api_v1_catalog_db import setup
from test_api_v1_db import api, problem, sign_in  # noqa: F401
from test_engagement_pure import make_vault
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
PHONE = "+919876543210"
REQUEST = {
    "kind": "appointment_request",
    "payload": {"preferred_date": "2026-10-20", "is_new_patient": True},
    "subject_name": "Ravi Kumar",
    "callback_number": PHONE,
}


def crm(api):  # type: ignore[no-untyped-def]
    app, engine, browser, headers, org, ws = setup(api)
    app.state.vault = make_vault()
    return app, engine, browser, headers, f"/api/v1/workspaces/{ws}"


def members(app, browser, headers, base, *roles):  # type: ignore[no-untyped-def]
    """Sign in one user per role and grant it in this workspace."""
    signed_in = []
    for role in roles:
        client, client_headers, user_id = sign_in(app, f"{role}@one.test")
        granted = browser.put(f"{base}/memberships/{user_id}", json={"role": role}, headers=headers)
        assert granted.status_code == 200, granted.text
        signed_in.append((client, client_headers, user_id))
    return signed_in


def test_work_item_lifecycle_without_leaking_personal_data(api):
    app, engine, browser, headers, base = crm(api)
    kinds = browser.get(f"{base}/work-item-kinds").json()["data"]  # installed with the workspace
    assert [(k["key"], k["initial_stage"]) for k in kinds] == [
        ("appointment_request", "new"),
        ("callback_request", "new"),
    ]
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

    retry = headers | {"Idempotency-Key": "call-42:appointment"}
    created = browser.post(
        f"{base}/work-items", json=REQUEST | {"entity_id": doctor}, headers=retry
    )
    assert created.status_code == 201, created.text
    item = created.json()["work_item"]
    assert created.headers["location"].endswith(item["id"])
    assert (item["stage"], item["open"], item["has_personal_details"]) == ("new", True, True)
    again = browser.post(f"{base}/work-items", json=REQUEST, headers=retry)
    assert again.json()["work_item"]["id"] == item["id"]  # a retried call makes no duplicate

    # Lists and details never contain personal data.
    listed = browser.get(f"{base}/work-items")
    detail = browser.get(f"{base}/work-items/{item['id']}")
    for response in (created, listed, detail):
        assert "Ravi" not in response.text and "9876543210" not in response.text

    # Validation: the kind's schema, E.164 numbers, and one kind of change per PATCH.
    body = problem(
        browser.post(
            f"{base}/work-items", json=REQUEST | {"payload": {"notes": "x"}}, headers=headers
        ),
        422,
    )
    assert "payload.notes" in {e["field"] for e in body["errors"]}
    assert "x" not in str(body["errors"])  # never echoes submitted values
    problem(
        browser.post(
            f"{base}/work-items", json=REQUEST | {"callback_number": "98765"}, headers=headers
        ),
        422,
    )
    path = f"{base}/work-items/{item['id']}"
    problem(
        browser.patch(
            path, json={"row_version": 1, "stage": "closed", "staff_note": "x"}, headers=headers
        ),
        422,
    )

    moved = browser.patch(path, json={"row_version": 1, "stage": "contacted"}, headers=headers)
    assert moved.json()["work_item"]["stage"] == "contacted"
    problem(browser.patch(path, json={"row_version": 1, "stage": "closed"}, headers=headers), 409)
    noted = browser.patch(
        path, json={"row_version": 2, "staff_note": "Prefers mornings."}, headers=headers
    )
    assert noted.status_code == 200 and "mornings" not in noted.text
    closed = browser.patch(path, json={"row_version": 3, "stage": "closed"}, headers=headers)
    history = closed.json()["history"]
    assert [(h["from_stage"], h["to_stage"]) for h in history] == [
        (None, "new"),
        ("new", "contacted"),
        ("contacted", "closed"),
    ]
    problem(browser.patch(path, json={"row_version": 4, "stage": "new"}, headers=headers), 409)
    assert browser.get(f"{base}/work-items", params={"open": "true"}).json()["data"] == []

    revealed = browser.post(f"{path}/reveal", headers=headers).json()
    assert revealed == {
        "subject_name": "Ravi Kumar",
        "callback_number": PHONE,
        "staff_note": "Prefers mornings.",
        "erased": False,
    }
    assert browser.delete(f"{path}/personal-details", headers=headers).status_code == 204
    assert browser.post(f"{path}/reveal", headers=headers).json()["erased"] is True
    assert browser.get(path).json()["work_item"]["personal_details_erased"] is True


def test_roles_isolation_and_assignment(api):
    app, engine, browser, headers, base = crm(api)
    item = browser.post(f"{base}/work-items", json=REQUEST, headers=headers).json()["work_item"]
    path = f"{base}/work-items/{item['id']}"
    (viewer, viewer_h, _), (staff, staff_h, staff_id) = members(
        app, browser, headers, base, "viewer", "staff"
    )

    assert viewer.get(path).status_code == 200
    problem(viewer.post(f"{path}/reveal", headers=viewer_h), 403)
    problem(viewer.post(f"{base}/work-items", json=REQUEST, headers=viewer_h), 403)
    assert staff.post(f"{path}/reveal", headers=staff_h).status_code == 200
    problem(staff.delete(f"{path}/personal-details", headers=staff_h), 403)  # admin+ only
    assignee = {"row_version": 1, "assignee_user_id": str(staff_id)}
    problem(staff.put(f"{path}/assignee", json=assignee, headers=staff_h), 403)  # manager+
    assigned = browser.put(f"{path}/assignee", json=assignee, headers=headers)
    assert assigned.json()["assignee_user_id"] == str(staff_id)
    mine = browser.get(f"{base}/work-items", params={"assignee_user_id": str(staff_id)})
    assert [i["id"] for i in mine.json()["data"]] == [item["id"]]

    # Someone from another organization sees nothing, not even that the item exists.
    _, other_engine, other, other_h, _, other_ws = setup(api, "two")
    problem(other.get(path), 404)
    problem(other.post(f"{path}/reveal", headers=other_h), 404)
    outsider = {"row_version": 2, "assignee_user_id": str(sign_in(app, "x@two.test")[2])}
    problem(browser.put(f"{path}/assignee", json=outsider, headers=headers), 422)
    problem(other.get(f"/api/v1/workspaces/{other_ws}/work-items/{item['id']}"), 404)

    with count_queries(engine) as one:
        browser.get(f"{base}/work-items")
    for n in range(4):
        browser.post(
            f"{base}/work-items",
            json={"kind": "callback_request", "payload": {"reason_category": "fees"}},
            headers=headers | {"Idempotency-Key": f"callback-{n:04}"},
        )
    with count_queries(engine) as five:
        assert len(browser.get(f"{base}/work-items").json()["data"]) == 5
    assert one and len(five) == len(one)

    # Reveal needs the encryption keys; without them the API says so instead of guessing.
    app.state.vault = None
    problem(browser.post(f"{path}/reveal", headers=headers), 503)


def test_contacts_consents_and_phone_search(api):
    app, engine, browser, headers, base = crm(api)
    created = browser.post(
        f"{base}/contacts",
        json={"display_name": "Asha Verma", "phone": PHONE, "preferred_language": "hi-IN"},
        headers=headers,
    )
    assert created.status_code == 201 and "Asha" not in created.text
    contact = created.json()
    problem(browser.post(f"{base}/contacts", json={"phone": PHONE}, headers=headers), 409)
    problem(browser.post(f"{base}/contacts", json={"phone": "12345"}, headers=headers), 422)

    found = browser.post(f"{base}/contacts/search", json={"phone": PHONE}, headers=headers)
    assert found.json()["id"] == contact["id"]
    missing = {"phone": "+919000000000"}
    problem(browser.post(f"{base}/contacts/search", json=missing, headers=headers), 404)

    path = f"{base}/contacts/{contact['id']}"
    consent = {"consent_type": "profile_reuse", "action": "granted", "notice_version": "2026-09"}
    granted = browser.post(f"{path}/consents", json=consent, headers=headers)
    assert granted.status_code == 201 and granted.json()["consent_status"] == "granted"
    problem(
        browser.post(f"{path}/consents", json=consent | {"source": "voice"}, headers=headers), 422
    )

    ((viewer, viewer_h, _),) = members(app, browser, headers, base, "viewer")
    assert [c["id"] for c in viewer.get(f"{base}/contacts").json()["data"]] == [contact["id"]]
    problem(viewer.post(f"{path}/reveal", headers=viewer_h), 403)
    revealed = browser.post(f"{path}/reveal", headers=headers).json()
    assert (revealed["display_name"], revealed["phone"]) == ("Asha Verma", PHONE)
    assert browser.delete(f"{path}/personal-details", headers=headers).status_code == 204
    problem(browser.post(f"{base}/contacts/search", json={"phone": PHONE}, headers=headers), 404)


def test_tasks_and_conversations(api):
    app, engine, browser, headers, base = crm(api)
    item = browser.post(f"{base}/work-items", json=REQUEST, headers=headers).json()["work_item"]
    task = browser.post(
        f"{base}/tasks",
        json={
            "title": "Call Ravi back",
            "work_item_id": item["id"],
            "due_at": "2026-10-19T10:00:00+05:30",
        },
        headers=headers,
    )
    assert task.status_code == 201, task.text
    path = f"{base}/tasks/{task.json()['id']}"
    naive = {"title": "x", "due_at": "2026-10-19T10:00:00"}
    problem(browser.post(f"{base}/tasks", json=naive, headers=headers), 422)
    stranger = {"title": "x", "assignee_user_id": str(sign_in(app, "x@two.test")[2])}
    problem(browser.post(f"{base}/tasks", json=stranger, headers=headers), 422)

    done = browser.patch(path, json={"row_version": 1, "status": "done"}, headers=headers)
    assert done.json()["status"] == "done" and done.json()["completed_at"] is not None
    problem(browser.patch(path, json={"row_version": 1, "status": "open"}, headers=headers), 409)
    linked = browser.get(f"{base}/tasks", params={"work_item_id": item["id"]}).json()["data"]
    assert [t["id"] for t in linked] == [task.json()["id"]]
    assert browser.get(f"{base}/tasks", params={"status": "open"}).json()["data"] == []

    # Conversations are written by the voice runtime; the API only reads them.
    assert browser.get(f"{base}/conversations", params={"needs_review": "true"}).json() == {
        "data": [],
        "page": {"limit": 50, "next_cursor": None},
    }
    problem(browser.get(f"{base}/conversations/{item['id']}"), 404)
