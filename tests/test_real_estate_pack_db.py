"""The real-estate pack runs on the same core as the clinic: no code knows either industry.

Real app, mocked Supabase Auth, real Postgres with forced RLS. Runs only when
PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (api, admin_id) are not redefinitions.
# ruff: noqa: F811
import asyncio
import json

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from test_api_v1_db import api, onboard, problem, sign_in  # noqa: F401
from test_engagement_pure import make_vault
from test_iam_tenancy_db import URL, admin_id, migrated  # noqa: F401

from praxima.packs import loader

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
PACK = loader.load("real_estate")
NUMBER = "+912233445566"


def register_pack() -> None:
    """What `scripts/packs.py register` does, for this pack only."""
    engine = create_sync_engine(URL)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenancy.pack_versions (pack_key, version, manifest, checksum) "
                "VALUES (:key, :version, CAST(:manifest AS jsonb), :checksum)"
            ),
            {
                "key": PACK.key,
                "version": PACK.version,
                "manifest": json.dumps(PACK.payload()),
                "checksum": PACK.checksum(),
            },
        )
    engine.dispose()


def test_a_real_estate_workspace_end_to_end(api, monkeypatch):
    import base64
    import os

    from praxima.runtime.release.knowledge import ReleaseKnowledge
    from praxima.runtime.release.loader import load_release

    register_pack()
    monkeypatch.setenv("PRAXIMA_RUNTIME_DATABASE_URL", URL)
    # The voice worker encrypts names and numbers itself before storing a request.
    monkeypatch.setenv(
        "CLINIC_PII_KEYS", json.dumps({"v1": base64.b64encode(os.urandom(32)).decode()})
    )
    monkeypatch.setenv("CLINIC_PII_KEY_VERSION", "v1")
    app, engine, admin = api
    app.state.vault = make_vault()  # staff-created requests may hold personal details
    browser, headers, owner = sign_in(app, "owner@homes.test")
    org, _ = onboard(engine, admin, "homes", owner)
    created = browser.post(
        f"/api/v1/organizations/{org}/workspaces",
        json={
            "slug": "sales-desk",
            "name": "Skyline Homes",
            "pack_key": "real_estate",
            "pack_version": PACK.version,
            "timezone": "Asia/Kolkata",
            "default_language": "hi-IN",
            "supported_languages": ["hi-IN", "en-IN"],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    ws = created.json()["id"]
    assert created.json()["industry"] == "real_estate"
    base = f"/api/v1/workspaces/{ws}"

    # The pack, not the code, defines the vocabulary.
    pack = browser.get(f"{base}/pack").json()
    assert pack["entity_labels"]["property"] == {"name": "Property", "plural_name": "Properties"}
    assert (
        "price_sheet" in pack["document_categories"] and pack["callback_kind"] == "callback_request"
    )
    types = [t["key"] for t in browser.get(f"{base}/entity-types").json()["data"]]
    assert types == ["project", "property", "sales_agent", "site_office"]
    kinds = [k["key"] for k in browser.get(f"{base}/work-item-kinds").json()["data"]]
    assert kinds == ["callback_request", "lead_inquiry", "site_visit_request"]

    def entity(kind: str, key: str, name: str, **attributes: object) -> str:
        response = browser.post(
            f"{base}/entities",
            json={"type": kind, "key": key, "name": name, "attributes": attributes},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        entity_id = response.json()["id"]
        browser.patch(
            f"{base}/entities/{entity_id}",
            json={"row_version": 1, "publication_status": "published"},
            headers=headers,
        )
        return entity_id

    project = entity(
        "project",
        "skyline-heights",
        "Skyline Heights",
        city="Pune",
        locality="Baner",
        status="under_construction",
        possession="2027-12",
    )
    unit = entity(
        "property",
        "sh-2bhk",
        "Skyline Heights 2 BHK",
        property_type="apartment",
        configuration="2 BHK",
        bedrooms=2,
        price_from=8500000,
        currency="INR",
        availability="few_left",
    )
    office = entity(
        "site_office", "baner-office", "Baner sales office", address="Baner Road", city="Pune"
    )
    # Clinic attributes are rejected: the pack's schema decides.
    wrong = browser.post(
        f"{base}/entities",
        json={
            "type": "property",
            "key": "x",
            "name": "X",
            "attributes": {"property_type": "apartment", "specialization": "ENT"},
        },
        headers=headers,
    )
    problem(wrong, 422)

    link_types = {r["key"] for r in browser.get(f"{base}/relation-types").json()["data"]}
    assert link_types == {"property_in_project", "sales_agent_for_project", "project_site_office"}
    for relation, source, target in (
        ("property_in_project", unit, project),
        ("project_site_office", project, office),
    ):
        link = browser.post(
            f"{base}/relations",
            json={"relation_type": relation, "from_entity_id": source, "to_entity_id": target},
            headers=headers,
        )
        assert link.status_code == 201, link.text
        browser.patch(
            f"{base}/relations/{link.json()['id']}",
            json={"publication_status": "published"},
            headers=headers,
        )
    hours = browser.post(
        f"{base}/availability-rules",
        json={
            "entity_id": office,
            "timezone": "Asia/Kolkata",
            "rrule": "FREQ=WEEKLY;BYDAY=SA,SU",
            "start_time": "10:00",
            "end_time": "18:00",
        },
        headers=headers,
    )
    assert hours.status_code == 201, hours.text
    browser.patch(
        f"{base}/availability-rules/{hours.json()['id']}",
        json={"publication_status": "published"},
        headers=headers,
    )

    # Staff take a lead by hand; its fields and stages come from the pack.
    lead = browser.post(
        f"{base}/work-items",
        json={
            "kind": "lead_inquiry",
            "entity_id": project,
            "payload": {"property_type": "apartment", "bedrooms": 2, "purpose": "self_use"},
        },
        headers=headers,
    )
    assert lead.status_code == 201, lead.text
    assert lead.json()["work_item"]["stage"] == "new"
    problem(
        browser.post(
            f"{base}/work-items",
            json={"kind": "appointment_request", "payload": {}},
            headers=headers,
        ),
        422,
    )

    # Publish to callers and answer a call from the release.
    agent = browser.post(
        f"{base}/agents",
        json={
            "name": "Sales Desk",
            "slug": "sales-desk",
            **{
                k: pack["agent_defaults"][k]
                for k in ("greeting_message", "emergency_message", "fallback_message")
            },
        },
        headers=headers,
    ).json()
    browser.post(
        f"{base}/agents/{agent['id']}/phone-numbers",
        json={"phone_number": NUMBER, "provider": "plivo"},
        headers=headers,
    )
    releases = f"{base}/agents/{agent['id']}/releases"
    preview = browser.post(f"{releases}/preview", headers=headers).json()
    assert preview["summary"]["entities"] == {"project": 1, "property": 1, "site_office": 1}
    published = browser.post(releases, json={"digest": preview["digest"]}, headers=headers)
    assert published.status_code == 201, published.text

    async def call() -> tuple[str, dict, dict, dict]:  # type: ignore[type-arg]
        knowledge = ReleaseKnowledge(await load_release(NUMBER))
        await knowledge.start_call(
            called_number=NUMBER, is_sip=True, attributes={"sip.callID": "SCL_homes1"}
        )
        found = await knowledge.find_entities("2 BHK apartment")
        detail = await knowledge.get_entity("Skyline Heights")
        visit = await knowledge.create_request(
            kind="site_visit_request",
            details_json=json.dumps({"preferred_date": "2026-10-24", "visitors": 2}),
            about="Skyline Heights",
            name="Meera",
            callback_number="+919812300000",
        )
        await knowledge.finish_call()
        return knowledge.instructions, found, detail, visit

    prompt, found, detail, visit = asyncio.run(call())
    assert "Skyline Homes" in prompt and "allotment" in prompt and "diagnose" not in prompt
    assert "site_visit_request" in prompt and "lead_inquiry" in prompt
    assert [e["name"] for e in found["entities"]][0] == "Skyline Heights 2 BHK"
    linked = {(link["relation"], link["with"]) for link in detail["links"]}
    assert linked == {
        ("property_in_project", "Skyline Heights 2 BHK"),
        ("project_site_office", "Baner sales office"),
    }
    assert visit["status"] == "recorded"
    inbox = browser.get(f"{base}/work-items", params={"kind": "site_visit_request"}).json()["data"]
    assert len(inbox) == 1 and inbox[0]["entity_id"] == project
    calls = browser.get(f"{base}/conversations").json()["data"]
    assert calls[0]["disposition"] == "request_created"
