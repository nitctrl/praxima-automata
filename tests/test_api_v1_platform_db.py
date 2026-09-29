"""Platform admin area: packs, every organization and platform admins (migration 0010).

Real app, mocked Supabase Auth, real Postgres with forced RLS. Runs only when
PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
import asyncio
import uuid

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from support.queries import count_queries
from test_api_v1_db import ORIGIN, api, client, onboard, problem, sign_in  # noqa: F401
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401

from praxima.packs import loader
from praxima.shared.db.engine import (
    Scope,
    create_engine,
    scoped_transaction,
    session_factory,
)

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
ADMIN = "admin@platform.test"  # seeded as a platform admin by the admin_id fixture
ROUTES = (
    ("GET", "/api/v1/platform/packs", None),
    ("POST", "/api/v1/platform/packs/real_estate/registrations", None),
    ("PATCH", "/api/v1/platform/packs/clinic/versions/1.0.0", {"status": "withdrawn"}),
    ("GET", "/api/v1/platform/organizations", None),
    ("GET", "/api/v1/platform/admins", None),
    ("POST", "/api/v1/platform/admins", {"email": "a@one.test"}),
    ("DELETE", f"/api/v1/platform/admins/{uuid.uuid4()}", None),
)


def owner_sql(statement: str, **values: object) -> None:
    engine = create_sync_engine(URL)
    with engine.begin() as connection:
        connection.execute(text(statement), values)
    engine.dispose()


def test_session_says_who_is_a_platform_admin(api):
    app, _, _ = api
    admin, _, _ = sign_in(app, ADMIN)
    assert admin.get("/api/v1/auth/session").json()["is_platform_admin"] is True
    someone, _, _ = sign_in(app, "someone@one.test")
    assert someone.get("/api/v1/auth/session").json()["is_platform_admin"] is False


def test_platform_area_is_hidden_from_everyone_else(api):
    app, _, _ = api
    browser, headers, _ = sign_in(app, "owner@one.test")
    for method, path, body in ROUTES:
        problem(browser.request(method, path, json=body, headers=headers), 404)
    problem(client(app).get("/api/v1/platform/packs"), 401)
    admin, admin_headers, _ = sign_in(app, ADMIN)
    # Writes still need the CSRF token and the dashboard origin.
    problem(admin.post("/api/v1/platform/packs/real_estate/registrations", headers=ORIGIN), 403)
    problem(
        admin.post(
            "/api/v1/platform/packs/real_estate/registrations",
            headers={"X-CSRF-Token": admin_headers["X-CSRF-Token"]},
        ),
        403,
    )


def test_register_packs_and_control_what_organizations_see(api):
    app, _, _ = api
    admin, headers, _ = sign_in(app, ADMIN)
    catalog = {p["key"]: p for p in admin.get("/api/v1/platform/packs").json()["data"]}
    assert set(catalog) == set(loader.available())
    clinic, estate = catalog["clinic"], catalog["real_estate"]
    assert clinic["needs_registration"] is False and clinic["versions"][0]["status"] == "available"
    assert estate["needs_registration"] is True and estate["versions"] == []

    first = admin.post("/api/v1/platform/packs/real_estate/registrations", headers=headers)
    assert first.status_code == 201, first.text
    assert first.json() == {
        "key": "real_estate",
        "version": loader.load("real_estate").version,
        "registered": True,
    }
    again = admin.post("/api/v1/platform/packs/real_estate/registrations", headers=headers)
    assert again.status_code == 200 and again.json()["registered"] is False
    problem(admin.post("/api/v1/platform/packs/hotel/registrations", headers=headers), 404)
    # Only known pack keys: never a path from the request.
    problem(admin.post("/api/v1/platform/packs/..clinic/registrations", headers=headers), 422)

    someone, _, _ = sign_in(app, "someone@one.test")

    def offered() -> set[str]:
        return {p["key"] for p in someone.get("/api/v1/packs").json()["data"]}

    assert offered() == {"clinic", "real_estate"}
    version = loader.load("real_estate").version
    path = f"/api/v1/platform/packs/real_estate/versions/{version}"
    withdrawn = admin.patch(path, json={"status": "withdrawn"}, headers=headers)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["versions"][0]["status"] == "withdrawn"
    assert offered() == {"clinic"}
    admin.patch(path, json={"status": "available"}, headers=headers)
    assert offered() == {"clinic", "real_estate"}

    problem(admin.patch(path, json={"status": "gone"}, headers=headers), 422)
    problem(
        admin.patch(
            "/api/v1/platform/packs/real_estate/versions/9.9.9",
            json={"status": "withdrawn"},
            headers=headers,
        ),
        404,
    )


def test_changed_files_need_a_version_bump(api):
    app, _, _ = api
    clinic = loader.load("clinic")
    owner_sql(
        "UPDATE tenancy.pack_versions SET checksum = 'stale' WHERE pack_key = 'clinic' "
        "AND version = :version",
        version=clinic.version,
    )
    admin, headers, _ = sign_in(app, ADMIN)
    entry = next(
        p for p in admin.get("/api/v1/platform/packs").json()["data"] if p["key"] == "clinic"
    )
    assert entry["files_changed_without_bump"] is True
    detail = problem(
        admin.post("/api/v1/platform/packs/clinic/registrations", headers=headers), 409
    )
    assert "Bump `version`" in detail["detail"]


def test_every_organization_with_constant_queries(api):
    app, engine, admin_id = api
    _, _, one = sign_in(app, "owner@one.test")
    onboard(engine, admin_id, "one", one)
    admin, _, _ = sign_in(app, ADMIN)
    with count_queries(engine) as few:
        listed = admin.get("/api/v1/platform/organizations").json()
    [org] = listed["data"]
    assert org["slug"] == "one" and org["member_count"] == 1
    assert [w["pack_key"] for w in org["workspaces"]] == ["clinic"]

    for slug in ("two", "three"):
        _, _, owner = sign_in(app, f"owner@{slug}.test")
        onboard(engine, admin_id, slug, owner)
    with count_queries(engine) as more:
        page = admin.get("/api/v1/platform/organizations?limit=2").json()
    assert len(few) == len(more)  # no N+1
    assert [o["slug"] for o in page["data"]] == ["three", "two"]
    rest = admin.get(f"/api/v1/platform/organizations?cursor={page['page']['next_cursor']}")
    assert [o["slug"] for o in rest.json()["data"]] == ["one"]

    # An owner sees only their own organization's workspaces, never the others.
    owner, _, _ = sign_in(app, "owner@one.test")
    assert [w["slug"] for w in owner.get("/api/v1/workspaces").json()["data"]] == ["main"]


def test_manage_platform_admins(api):
    app, _, admin_id = api
    admin, headers, _ = sign_in(app, ADMIN)
    _, _, helper_id = sign_in(app, "helper@one.test")
    problem(
        admin.post("/api/v1/platform/admins", json={"email": "nobody@x.test"}, headers=headers),
        404,
    )
    added = admin.post(
        "/api/v1/platform/admins", json={"email": "Helper@one.test"}, headers=headers
    )
    assert added.status_code == 201, added.text
    assert added.json()["user_id"] == str(helper_id)
    listed = admin.get("/api/v1/platform/admins").json()["data"]
    assert [a["user_id"] for a in listed] == [str(admin_id), str(helper_id)]

    helper, helper_headers, _ = sign_in(app, "helper@one.test")
    assert helper.get("/api/v1/platform/packs").status_code == 200
    problem(admin.delete(f"/api/v1/platform/admins/{admin_id}", headers=headers), 409)  # self
    assert (
        helper.delete(f"/api/v1/platform/admins/{admin_id}", headers=helper_headers).status_code
        == 204
    )
    problem(  # never yourself (so the last admin always stays)
        helper.delete(f"/api/v1/platform/admins/{helper_id}", headers=helper_headers), 409
    )
    problem(admin.get("/api/v1/platform/packs"), 404)  # no longer an admin


def test_admin_functions_refuse_anyone_else(admin_id):
    stranger = uuid.uuid4()
    statements = (
        "SELECT tenancy.admin_register_pack_version('x', '1.0.0', '{}'::jsonb, 'c')",
        "SELECT tenancy.admin_set_pack_status('clinic', '1.0.0', 'withdrawn')",
        "SELECT * FROM iam.admin_list_platform_admins()",
        f"SELECT iam.admin_grant_platform_admin('{stranger}')",
        f"SELECT iam.admin_revoke_platform_admin('{admin_id}')",
    )

    async def attempt(statement: str) -> None:
        engine = create_engine(URL, pooled=False)
        try:
            async with scoped_transaction(
                session_factory(engine), Scope(user_id=stranger)
            ) as session:
                await session.execute(text(statement))
        finally:
            await engine.dispose()

    for statement in statements:
        with pytest.raises(DBAPIError) as refused:
            asyncio.run(attempt(statement))
        assert getattr(refused.value.orig, "sqlstate", None) == "42501"
