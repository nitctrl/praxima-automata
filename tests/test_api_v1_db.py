"""/api/v1 end to end: real app, mocked Supabase Auth, real Postgres (RLS on).

Runs only when PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

import asyncio
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from support.queries import count_queries
from test_iam_tenancy_db import DRAFT, URL, admin_id, migrated  # noqa: F401

from praxima.entrypoints.api import SupabaseGateway, WebSettings, create_app
from praxima.modules import iam, tenancy
from praxima.shared.db.engine import Scope, create_engine, scoped_transaction, session_factory

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
CONFIG = WebSettings("https://example.supabase.co", "sb_publishable_test", "https://testserver")
ORIGIN = {"Origin": CONFIG.origin}


def subject(email: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, email))


def supabase(request: httpx.Request) -> httpx.Response:
    """Password "right" signs in any email; tokens encode the email."""
    if request.url.path == "/auth/v1/token":
        values = json.loads(request.content)
        if values["password"] != "right":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "tok:" + values["email"]})
    if request.url.path == "/auth/v1/user":
        email = request.headers["Authorization"].removeprefix("Bearer tok:")
        return httpx.Response(
            200, json={"id": subject(email), "email": email, "email_confirmed_at": "2026-01-01"}
        )
    return httpx.Response(404)


@pytest.fixture
def api(admin_id):  # noqa: F811
    engine = create_engine(URL, pooled=False)
    app = create_app(
        CONFIG,
        gateway=SupabaseGateway(CONFIG, httpx.MockTransport(supabase)),
        api_sessions=session_factory(engine),
    )
    yield app, engine, admin_id
    asyncio.run(engine.dispose())


def client(app) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(app, base_url=CONFIG.origin)


def sign_in(app, email: str) -> tuple[TestClient, dict[str, str], uuid.UUID]:  # type: ignore[no-untyped-def]
    browser = client(app)
    response = browser.post(
        "/api/v1/auth/session", json={"email": email, "password": "right"}, headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return browser, ORIGIN | {"X-CSRF-Token": body["csrf"]}, uuid.UUID(body["user"]["id"])


def onboard(engine, admin: uuid.UUID, slug: str, owner: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:  # type: ignore[no-untyped-def]
    async def go() -> tuple[uuid.UUID, uuid.UUID]:
        sessions = session_factory(create_engine(URL, pooled=False))
        async with scoped_transaction(sessions, Scope(user_id=admin)) as session:
            org = await tenancy.create_organization(
                session,
                iam.Actor(admin, None, is_platform_admin=True),
                slug=slug,
                name=slug.title(),
                owner_user_id=owner,
            )
        async with scoped_transaction(sessions, Scope(user_id=owner, organization_id=org)) as s:
            ws = await tenancy.create_workspace(
                s, iam.Actor(owner, "owner"), organization_id=org, draft=DRAFT
            )
        return org, ws

    return asyncio.run(go())


def problem(response: httpx.Response, status: int) -> dict:  # type: ignore[type-arg]
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["request_id"] == response.headers["x-request-id"]
    return body


def test_sign_in_restore_and_sign_out(api):
    app, _, _ = api
    wrong = client(app).post(
        "/api/v1/auth/session", json={"email": "a@x.test", "password": "nope"}, headers=ORIGIN
    )
    assert problem(wrong, 401)["detail"] == "Email or password is incorrect."
    problem(
        client(app).post("/api/v1/auth/session", json={"email": "a@x.test", "password": "right"}),
        403,
    )

    browser = client(app)
    response = browser.post(
        "/api/v1/auth/session", json={"email": "A@X.test", "password": "right"}, headers=ORIGIN
    )
    cookie = response.headers["set-cookie"]
    assert "praxima_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "SameSite=strict" in cookie and "Path=/api" in cookie
    assert "tok:" not in cookie + response.text  # provider token never leaves the server
    body = response.json()
    assert body["user"]["email"] == "a@x.test" and body["memberships"] == []

    assert browser.get("/api/v1/auth/session").json()["user"] == body["user"]
    problem(browser.delete("/api/v1/auth/session", headers=ORIGIN), 403)  # no CSRF token
    assert (
        browser.delete(
            "/api/v1/auth/session", headers=ORIGIN | {"X-CSRF-Token": body["csrf"]}
        ).status_code
        == 204
    )
    problem(browser.get("/api/v1/auth/session"), 401)


def test_workspaces_are_isolated_versioned_and_validated(api):
    app, engine, admin = api
    a, a_headers, a_id = sign_in(app, "a@one.test")
    b, _, b_id = sign_in(app, "b@two.test")
    _, ws_a = onboard(engine, admin, "one", a_id)
    _, ws_b = onboard(engine, admin, "two", b_id)

    assert [w["id"] for w in a.get("/api/v1/workspaces").json()["data"]] == [str(ws_a)]
    problem(a.get(f"/api/v1/workspaces/{ws_b}"), 404)
    problem(b.get(f"/api/v1/workspaces/{ws_a}"), 404)
    problem(client(app).get("/api/v1/workspaces"), 401)

    path = f"/api/v1/workspaces/{ws_a}"
    assert a.get(path).json()["row_version"] == 1
    renamed = a.patch(path, json={"row_version": 1, "name": "Renamed"}, headers=a_headers)
    assert renamed.status_code == 200 and renamed.json()["row_version"] == 2
    assert renamed.json()["name"] == "Renamed"
    problem(a.patch(path, json={"row_version": 1, "name": "Late"}, headers=a_headers), 409)
    unknown = problem(
        a.patch(path, json={"row_version": 2, "colour": "red"}, headers=a_headers), 422
    )
    assert [e["field"] for e in unknown["errors"]] == ["colour"]
    problem(
        a.patch(path, json={"row_version": 2, "timezone": "Mars/Olympus"}, headers=a_headers), 422
    )
    problem(
        a.patch(path, json={"row_version": 2, "default_language": "ta-IN"}, headers=a_headers), 422
    )
    problem(b.patch(path, json={"row_version": 2, "name": "Hijack"}, headers=ORIGIN), 403)  # CSRF


def test_members_roles_and_query_budget(api):
    app, engine, admin = api
    a, a_headers, a_id = sign_in(app, "owner@one.test")
    org, ws = onboard(engine, admin, "one", a_id)
    members = f"/api/v1/workspaces/{ws}/members"

    def grant(user_id: uuid.UUID, role: str) -> httpx.Response:
        return a.put(
            f"/api/v1/workspaces/{ws}/memberships/{user_id}", json={"role": role}, headers=a_headers
        )

    c, c_headers, c_id = sign_in(app, "c@one.test")
    assert grant(c_id, "staff").json()["role"] == "staff"
    problem(grant(c_id, "owner"), 422)  # owners are organization-wide only
    with count_queries(engine) as one_member:
        listed = a.get(members).json()
    assert [(m["email"], m["role"]) for m in listed["data"]] == [("c@one.test", "staff")]

    for n in range(4):
        grant(sign_in(app, f"m{n}@one.test")[2], "viewer")
    with count_queries(engine) as five_members:
        assert len(a.get(members).json()["data"]) == 5
    assert one_member and len(five_members) == len(one_member)  # no N+1: flat cost
    print(f"GET members: {len(one_member)} statements for 1 member, {len(five_members)} for 5")

    problem(c.get(members), 403)  # staff can't read the member list
    problem(
        c.patch(
            f"/api/v1/workspaces/{ws}", json={"row_version": 1, "name": "x"}, headers=c_headers
        ),
        403,
    )

    assert (
        a.delete(f"/api/v1/workspaces/{ws}/memberships/{c_id}", headers=a_headers).status_code
        == 204
    )
    assert "c@one.test" not in [m["email"] for m in a.get(members).json()["data"]]
    assert c.get("/api/v1/workspaces").json()["data"] == []

    d, d_headers, d_id = sign_in(app, "d@one.test")
    admin_grant = a.put(
        f"/api/v1/organizations/{org}/memberships/{d_id}", json={"role": "admin"}, headers=a_headers
    )
    assert admin_grant.status_code == 200
    org_members = d.get(f"/api/v1/organizations/{org}/members").json()["data"]
    assert {(m["email"], m["role"]) for m in org_members} == {
        ("owner@one.test", "owner"),
        ("d@one.test", "admin"),
    }
    escalate = d.put(
        f"/api/v1/organizations/{org}/memberships/{d_id}", json={"role": "owner"}, headers=d_headers
    )
    assert problem(escalate, 403)["detail"] == "You can't grant a role higher than your own."


def test_v1_without_a_database_is_unavailable(admin_id):  # noqa: F811
    app = create_app(CONFIG, gateway=SupabaseGateway(CONFIG, httpx.MockTransport(supabase)))
    response = client(app).post(
        "/api/v1/auth/session", json={"email": "a@x.test", "password": "right"}, headers=ORIGIN
    )
    assert problem(response, 503)["detail"] == "This service is not configured."
