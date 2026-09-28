"""Self-service sign-up end to end: register, create your organization, create a workspace.

Real app, mocked Supabase Auth, real Postgres with forced RLS. Runs only when
PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (admin_id) are not redefinitions.
# ruff: noqa: F811
import asyncio
import json
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_api_v1_catalog_db import WORKSPACE
from test_api_v1_db import CONFIG, ORIGIN, client, problem, subject, supabase
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401

from praxima.entrypoints.api import SupabaseGateway, create_app
from praxima.shared.db.engine import (
    Scope,
    apply_scope,
    create_engine,
    scoped_transaction,
    session_factory,
)

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
PASSWORD = "long-enough-1"


def provider(request: httpx.Request) -> httpx.Response:
    """Sign-up: "confirm@…" must confirm by email, "taken@…" already exists; others sign in."""
    if request.url.path == "/auth/v1/signup":
        values = json.loads(request.content)
        email = values["email"]
        if email.startswith("taken@"):
            return httpx.Response(422, json={"error_code": "user_already_exists"})
        user = {
            "id": subject(email),
            "email": email,
            "user_metadata": values["data"],
        }
        if email.startswith("confirm@"):
            return httpx.Response(200, json=user | {"confirmation_sent_at": "2026-09-27"})
        return httpx.Response(
            200,
            json={
                "access_token": "tok:" + email,
                "user": user | {"email_confirmed_at": "2026-09-27"},
            },
        )
    return supabase(request)


def make_app(self_signup: bool):  # type: ignore[no-untyped-def]
    engine = create_engine(URL, pooled=False)
    app = create_app(
        CONFIG,
        gateway=SupabaseGateway(CONFIG, httpx.MockTransport(provider)),
        api_sessions=session_factory(engine),
        self_signup=self_signup,
    )
    return app, engine


def register(browser, email: str, password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return browser.post(
        "/api/v1/auth/registrations",
        json={"email": email, "password": password, "display_name": "Asha Rao"},
        headers=ORIGIN,
    )


def test_register_create_organization_and_workspace(admin_id):
    app, engine = make_app(self_signup=True)
    try:
        browser = client(app)
        created = register(browser, "asha@new.test")
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "signed_in"
        assert body["session"]["user"]["display_name"] == "Asha Rao"
        assert body["session"]["memberships"] == []
        headers = ORIGIN | {"X-CSRF-Token": body["session"]["csrf"]}

        # Signed in by the registration itself: the cookie works.
        assert browser.get("/api/v1/auth/session").json()["user"]["email"] == "asha@new.test"

        org = browser.post(
            "/api/v1/organizations", json={"name": "Asha Clinic", "slug": "asha"}, headers=headers
        )
        assert org.status_code == 201, org.text
        memberships = browser.get("/api/v1/auth/session").json()["memberships"]
        assert [(m["organization_id"], m["workspace_id"], m["role"]) for m in memberships] == [
            (org.json()["id"], None, "owner")
        ]

        # One self-serve organization per person, and the owner can now create workspaces.
        again = browser.post(
            "/api/v1/organizations", json={"name": "Second", "slug": "second"}, headers=headers
        )
        problem(again, 409)
        workspace = browser.post(
            f"/api/v1/organizations/{org.json()['id']}/workspaces", json=WORKSPACE, headers=headers
        )
        assert workspace.status_code == 201, workspace.text
        assert [w["id"] for w in browser.get("/api/v1/workspaces").json()["data"]] == [
            workspace.json()["id"]
        ]

        # Validation and CSRF.
        problem(register(client(app), "short@new.test", password="short"), 422)
        problem(
            browser.post(
                "/api/v1/organizations", json={"name": "X", "slug": "x-y"}, headers=ORIGIN
            ),
            403,
        )
        taken = problem(register(client(app), "taken@new.test"), 422)
        assert "sign in" in taken["detail"]
    finally:
        asyncio.run(engine.dispose())


def test_email_confirmation_and_duplicate_short_names(admin_id):
    app, engine = make_app(self_signup=True)
    try:
        pending = register(client(app), "confirm@new.test")
        assert pending.status_code == 201
        assert pending.json() == {"status": "confirmation_required", "session": None}
        assert "praxima_session" not in pending.cookies

        def own_org(email: str) -> httpx.Response:
            browser = client(app)
            csrf = register(browser, email).json()["session"]["csrf"]
            return browser.post(
                "/api/v1/organizations",
                json={"name": "Shared", "slug": "shared"},
                headers=ORIGIN | {"X-CSRF-Token": csrf},
            )

        assert own_org("one@new.test").status_code == 201
        problem(own_org("two@new.test"), 409)  # short names are unique platform-wide
    finally:
        asyncio.run(engine.dispose())


def test_sign_up_is_off_unless_enabled(admin_id):
    app, engine = make_app(self_signup=False)
    try:
        problem(register(client(app), "asha@new.test"), 403)
        browser = client(app)
        signed_in = browser.post(
            "/api/v1/auth/session",
            json={"email": "someone@new.test", "password": "right"},
            headers=ORIGIN,
        ).json()
        blocked = browser.post(
            "/api/v1/organizations",
            json={"name": "Nope", "slug": "nope"},
            headers=ORIGIN | {"X-CSRF-Token": signed_in["csrf"]},
        )
        problem(blocked, 403)
    finally:
        asyncio.run(engine.dispose())


def test_database_limits_self_serve_organizations(admin_id):
    """RLS alone: only for yourself, and only while you belong to no organization."""
    user = uuid.uuid4()
    org = uuid.uuid4()
    insert = text(
        "INSERT INTO tenancy.organizations (id, slug, name, created_by) "
        "VALUES (:id, :slug, 'X', :creator)"
    )

    async def scenario() -> None:
        engine = create_engine(URL, pooled=False)
        sessions = session_factory(engine)
        try:
            with pytest.raises(DBAPIError, match="row-level security"):
                async with scoped_transaction(sessions, Scope(user_id=user)) as session:
                    await session.execute(
                        insert, {"id": uuid.uuid4(), "slug": "for-someone", "creator": admin_id}
                    )
            async with scoped_transaction(sessions, Scope(user_id=user)) as session:
                await session.execute(
                    text("INSERT INTO iam.users (id, email) VALUES (:id, 'self@new.test')"),
                    {"id": user},
                )
                await session.execute(insert, {"id": org, "slug": "mine", "creator": user})
                await apply_scope(session, Scope(user_id=user, organization_id=org))
                await session.execute(
                    text(
                        "INSERT INTO iam.memberships (id, user_id, organization_id, role) "
                        "VALUES (gen_random_uuid(), :user, :org, 'owner')"
                    ),
                    {"user": user, "org": org},
                )
            with pytest.raises(DBAPIError, match="row-level security"):
                async with scoped_transaction(sessions, Scope(user_id=user)) as session:
                    await session.execute(
                        insert, {"id": uuid.uuid4(), "slug": "second", "creator": user}
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())
