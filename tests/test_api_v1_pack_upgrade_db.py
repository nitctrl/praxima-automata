"""Upgrading a workspace's pack, and suspending an organization (migration 0011).

Real app, mocked Supabase Auth, real Postgres with forced RLS. Runs only when
PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
import asyncio
import json
from typing import Any

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from test_api_v1_catalog_db import MESSAGES, setup
from test_api_v1_db import api, problem, sign_in  # noqa: F401
from test_api_v1_releases_db import content
from test_iam_tenancy_db import CLINIC, URL, admin_id, migrated  # noqa: F401

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")


def register(version: str, edit: Any = None) -> None:
    """Register a newer clinic version (as the owner, like scripts/packs.py)."""
    payload = CLINIC.payload() | {"version": version}
    if edit:
        edit(payload)
    engine = create_sync_engine(URL)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenancy.pack_versions (pack_key, version, manifest, checksum) "
                "VALUES ('clinic', :version, CAST(:manifest AS jsonb), :version)"
            ),
            {"version": version, "manifest": json.dumps(payload)},
        )
    engine.dispose()


def add_lab_tests(payload: dict[str, Any]) -> None:
    doctor = payload["entity_types"][0]
    payload["entity_types"].append(doctor | {"key": "lab_test", "name": "Lab test"})


def change_doctor_fields(payload: dict[str, Any]) -> None:
    add_lab_tests(payload)
    for t in payload["entity_types"]:
        if t["key"] == "doctor":
            t["attributes_schema"] = t["attributes_schema"] | {"required": ["room"]}


def test_upgrade_to_a_newer_compatible_version(api):
    app, _, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    assert browser.get(f"{base}/pack").json()["upgrades"] == []
    register("9.0.0", add_lab_tests)
    register("9.1.0", change_doctor_fields)

    upgrades = browser.get(f"{base}/pack").json()["upgrades"]
    assert [(u["version"], u["problems"]) for u in upgrades] == [
        ("9.0.0", []),
        ("9.1.0", ["Changes the 'doctor' fields without a new schema_version."]),
    ]
    row_version = browser.get(base).json()["row_version"]

    manager, manager_headers, manager_id = sign_in(app, "manager@one.test")
    browser.put(f"{base}/memberships/{manager_id}", json={"role": "manager"}, headers=headers)
    upgrade = {"version": "9.0.0", "row_version": row_version}
    problem(manager.post(f"{base}/pack-upgrades", json=upgrade, headers=manager_headers), 403)
    problem(
        browser.post(f"{base}/pack-upgrades", json=upgrade | {"row_version": 99}, headers=headers),
        409,
    )
    blocked = problem(
        browser.post(
            f"{base}/pack-upgrades", json={**upgrade, "version": "9.1.0"}, headers=headers
        ),
        422,
    )
    assert "without a new schema_version" in blocked["detail"]
    problem(
        browser.post(
            f"{base}/pack-upgrades", json={**upgrade, "version": "8.0.0"}, headers=headers
        ),
        422,
    )

    upgraded = browser.post(f"{base}/pack-upgrades", json=upgrade, headers=headers)
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["pack_version"] == "9.0.0"
    types = {t["key"] for t in browser.get(f"{base}/entity-types").json()["data"]}
    assert "lab_test" in types  # what the new version adds is installed
    assert browser.get(f"{base}/pack").json()["version"] == "9.0.0"
    again = {"version": "9.0.0", "row_version": upgraded.json()["row_version"]}
    problem(browser.post(f"{base}/pack-upgrades", json=again, headers=headers), 422)


def test_suspended_organization_is_locked_but_kept(api, monkeypatch):
    from praxima.ai.release.loader import LoadedRelease, NoRelease, load_release

    monkeypatch.setenv("PRAXIMA_RUNTIME_DATABASE_URL", URL)
    app, _, browser, headers, org, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    agent = browser.post(
        f"{base}/agents", json={"name": "Desk", "slug": "desk", **MESSAGES}, headers=headers
    ).json()
    content(browser, headers, base)
    number = "+912212345679"
    browser.post(
        f"{base}/agents/{agent['id']}/phone-numbers",
        json={"phone_number": number, "provider": "plivo"},
        headers=headers,
    )
    path = f"{base}/agents/{agent['id']}/releases"
    digest = browser.post(f"{path}/preview", headers=headers).json()["digest"]
    browser.post(path, json={"digest": digest}, headers=headers)
    assert isinstance(asyncio.run(load_release(number)), LoadedRelease)

    admin, admin_headers, _ = sign_in(app, "admin@platform.test")
    problem(
        browser.patch(
            f"/api/v1/platform/organizations/{org}", json={"status": "suspended"}, headers=headers
        ),
        404,  # owners can't suspend or reactivate themselves
    )
    suspended = admin.patch(
        f"/api/v1/platform/organizations/{org}",
        json={"status": "suspended"},
        headers=admin_headers,
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == "suspended"

    listed = browser.get("/api/v1/workspaces").json()["data"]
    assert {w["organization_status"] for w in listed} == {"suspended"}
    assert "suspended" in problem(browser.get(base), 403)["detail"]
    problem(browser.get(f"/api/v1/organizations/{org}/members"), 403)
    assert admin.get(base).status_code == 200  # the platform team can still help
    assert asyncio.run(load_release(number)) == NoRelease("organization_inactive")

    admin.patch(
        f"/api/v1/platform/organizations/{org}", json={"status": "active"}, headers=admin_headers
    )
    assert browser.get(base).json()["organization_status"] == "active"
    assert isinstance(asyncio.run(load_release(number)), LoadedRelease)
    problem(
        admin.patch(
            f"/api/v1/platform/organizations/{org}",
            json={"status": "closed"},
            headers=admin_headers,
        ),
        422,
    )


def test_tools_a_pack_upgrade_adds_appear_switched_off(api):
    _, _, browser, headers, _, ws = setup(api)
    base = f"/api/v1/workspaces/{ws}"
    agent = browser.post(
        f"{base}/agents", json={"name": "Desk", "slug": "desk", **MESSAGES}, headers=headers
    ).json()
    register("9.0.0", lambda payload: payload["tools"].append("transfer_to_human"))
    row_version = browser.get(base).json()["row_version"]
    upgraded = browser.post(
        f"{base}/pack-upgrades",
        json={"version": "9.0.0", "row_version": row_version},
        headers=headers,
    )
    assert upgraded.status_code == 200, upgraded.text

    def tools() -> dict[str, bool]:
        found = browser.get(f"{base}/agents/{agent['id']}").json()["tools"]
        return {t["key"]: t["enabled"] for t in found}

    assert tools()["transfer_to_human"] is False  # offered now, but staff opt in
    assert tools()["find_entities"] is True
    switched = browser.put(
        f"{base}/agents/{agent['id']}/tools/transfer_to_human",
        json={"enabled": True},
        headers=headers,
    )
    assert switched.status_code in (200, 204), switched.text
    assert tools()["transfer_to_human"] is True
