"""/api/v1 knowledge end to end: real app, mocked Supabase Auth, real Postgres, fake Qdrant.

Runs only when PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
from datetime import datetime, timedelta, timezone

import pytest
from support.queries import count_queries
from test_api_v1_catalog_db import setup
from test_api_v1_db import api, problem, sign_in  # noqa: F401
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401
from test_knowledge_db import DOC, FakeIndex

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
RAW = {"Content-Type": "application/octet-stream"}


def upload(browser, headers, url, data=DOC, filename="about.md", **params):  # type: ignore[no-untyped-def]
    return browser.post(
        url, content=data, headers=headers | RAW, params={"filename": filename, **params}
    )


def test_upload_review_publish_and_search(api):
    app, engine, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    index = FakeIndex()
    app.state.knowledge_index = index

    created = upload(browser, headers, f"{base}/documents", category="about")
    assert created.status_code == 201, created.text
    body = created.json()
    version, sections = body["version"], body["sections"]
    assert version["status"] == "needs_review" and [s["heading"] for s in sections] == [
        "About us",
        "Timings",
    ]
    assert created.headers["location"].endswith(f"/versions/{version['id']}")

    # Upload rules: raw bytes only, known category, no duplicates, supported file types.
    json_upload = browser.post(
        f"{base}/documents",
        json={"x": 1},
        headers=headers,
        params={"filename": "a.md", "category": "about"},
    )
    problem(json_upload, 415)
    problem(
        upload(browser, headers, f"{base}/documents", category="recipes", data=b"# R\n\nx"), 422
    )
    problem(upload(browser, headers, f"{base}/documents", category="about"), 409)
    problem(upload(browser, headers, f"{base}/documents", category="about", filename="a.exe"), 422)
    too_big = upload(browser, headers, f"{base}/documents", category="about", data=b"#" * (6 << 20))
    problem(too_big, 413)

    doc = f"{base}/documents/{version['document_id']}"
    path = f"{doc}/versions/{version['id']}"
    reviewed = browser.put(
        f"{path}/sections",
        json={
            "row_version": version["row_version"],
            "sections": [
                {"heading": "About us", "text": "Shanti Clinic has served the city since 1998."},
                {"heading": "Timings", "text": "Open 9 to 5. Closed on Sundays."},
            ],
        },
        headers=headers,
    )
    assert reviewed.status_code == 200
    row_version = reviewed.json()["version"]["row_version"]
    problem(
        browser.patch(path, json={"row_version": 1, "status": "published"}, headers=headers), 409
    )
    published = browser.patch(
        path, json={"row_version": row_version, "status": "published"}, headers=headers
    )
    assert published.json()["status"] == "published"
    assert len(index.upserted) == 2

    hits = browser.get(f"{base}/knowledge/search", params={"q": "closed sundays"}).json()["data"]
    assert [h["heading"] for h in hits] == ["Timings"]
    index.fail = True  # Qdrant down: keyword search still answers
    assert browser.get(f"{base}/knowledge/search", params={"q": "sundays"}).json()["data"]

    # A new version reviews separately and supersedes the live one on publish.
    v2 = upload(browser, headers, f"{doc}/versions", data=DOC + b"\nWalk-ins welcome.\n")
    assert v2.status_code == 201 and v2.json()["version"]["version_no"] == 2
    detail = browser.get(doc).json()
    assert detail["document"]["published_version_no"] == 1
    assert [v["status"] for v in detail["versions"]] == ["needs_review", "published"]
    other = upload(
        browser, headers, f"{base}/documents", data=b"# Fees\n\nLow fees.", category="about"
    )
    other_doc = other.json()["version"]["document_id"]
    # A real version, requested under a different real document: 404, not someone else's data.
    problem(browser.get(f"{base}/documents/{other_doc}/versions/{version['id']}"), 404)
    wrong = {"row_version": 1, "status": "rejected"}
    problem(
        browser.patch(
            f"{base}/documents/{other_doc}/versions/{version['id']}", json=wrong, headers=headers
        ),
        404,
    )

    archive = browser.delete(
        doc, params={"row_version": detail["document"]["row_version"]}, headers=headers
    )
    assert archive.status_code == 204
    assert browser.get(f"{base}/knowledge/search", params={"q": "sundays"}).json()["data"] == []


def test_faqs_announcements_roles_and_budget(api):
    app, engine, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    faq = browser.post(
        f"{base}/faqs",
        json={"canonical_question": "Do you take insurance?", "approved_answer": "Yes."},
        headers=headers,
    )
    assert faq.status_code == 201 and faq.json()["publication_status"] == "draft"
    faq_path = f"{base}/faqs/{faq.json()['id']}"
    published = browser.patch(
        faq_path, json={"row_version": 1, "publication_status": "published"}, headers=headers
    )
    assert published.json()["publication_status"] == "published"
    assert [
        f["id"] for f in browser.get(f"{base}/faqs", params={"status": "published"}).json()["data"]
    ] == [faq.json()["id"]]

    now = datetime.now(timezone.utc)
    closure = browser.post(
        f"{base}/announcements",
        json={
            "kind": "closure",
            "public_message": "Closed today for Diwali.",
            "starts_at": (now - timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(hours=4)).isoformat(),
        },
        headers=headers,
    )
    assert closure.status_code == 201
    naive = {
        "kind": "closure",
        "public_message": "x",
        "starts_at": "2026-10-20T09:00:00",
        "ends_at": "2026-10-20T10:00:00",
    }
    problem(browser.post(f"{base}/announcements", json=naive, headers=headers), 422)
    active = browser.get(f"{base}/announcements", params={"active": "true"}).json()["data"]
    assert [a["id"] for a in active] == [closure.json()["id"]]

    viewer, viewer_headers, viewer_id = sign_in(app, "viewer@one.test")
    staff, staff_headers, staff_id = sign_in(app, "staff@one.test")
    for user_id, role in ((viewer_id, "viewer"), (staff_id, "staff")):
        browser.put(f"{base}/memberships/{user_id}", json={"role": role}, headers=headers)
    assert viewer.get(f"{base}/faqs").status_code == 200
    problem(
        viewer.post(
            f"{base}/faqs",
            json={"canonical_question": "Q?", "approved_answer": "A."},
            headers=viewer_headers,
        ),
        403,
    )
    problem(
        staff.patch(
            faq_path,
            json={"row_version": 2, "publication_status": "archived"},
            headers=staff_headers,
        ),
        403,
    )

    with count_queries(engine) as one:
        browser.get(f"{base}/faqs")
    for n in range(4):
        browser.post(
            f"{base}/faqs",
            json={"canonical_question": f"Question {n}?", "approved_answer": "Answer."},
            headers=headers,
        )
    with count_queries(engine) as five:
        assert len(browser.get(f"{base}/faqs").json()["data"]) == 5
    assert one and len(five) == len(one)

    assert browser.delete(faq_path, params={"row_version": 2}, headers=headers).status_code == 204
    problem(browser.get(faq_path), 404)
