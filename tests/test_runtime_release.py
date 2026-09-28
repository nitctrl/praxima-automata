"""Voice answers from a pinned release snapshot (step 4b), without LiveKit or a database."""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from praxima.modules.releases.domain.agent_snapshot import AgentSnapshot
from praxima.runtime.prompting import render_release_prompt
from praxima.runtime.release import lookup
from praxima.runtime.release.loader import LoadedRelease, NoRelease, interpret

IST = ZoneInfo("Asia/Kolkata")
# Monday 2026-10-19, 11:00 in Mumbai.
NOW = datetime(2026, 10, 19, 11, 0, tzinfo=IST)


def _entity(id: str, type: str, name: str, **attributes: object) -> dict:  # type: ignore[type-arg]
    key = name.lower().replace(" ", "-").replace(".", "")
    return {
        "id": id,
        "type": type,
        "key": key,
        "name": name,
        "aliases": [],
        "attributes": attributes,
    }


RAW = {
    "schema_version": 4,
    "pack": {"key": "clinic", "version": "1.0.0"},
    "workspace": {
        "name": "Sunrise Clinic",
        "timezone": "Asia/Kolkata",
        "default_language": "hi-IN",
        "supported_languages": ["hi-IN", "en-IN"],
    },
    "agent": {
        "id": "a1",
        "name": "Sunrise Reception",
        "persona": "Warm and brief.",
        "greeting": "Namaste! Sunrise Clinic mein aapka swagat hai.",
        "emergency_message": "Please call 112 now.",
        "fallback_message": "Our team will call you back.",
        "transfer_enabled": False,
        "prompt_version": 1,
    },
    "tools": [{"key": k} for k in ("find_entities", "get_availability", "search_knowledge")],
    "entity_types": [
        {
            "key": "doctor",
            "name": "Doctor",
            "schema_version": 1,
            "searchable_fields": ["name", "specialization"],
        },
        {
            "key": "service",
            "name": "Service",
            "schema_version": 1,
            "searchable_fields": ["name", "category"],
        },
    ],
    "entities": [
        _entity("d1", "doctor", "Dr. Asha Sharma", specialization="Cardiology"),
        _entity("d2", "doctor", "Dr. Ravi Menon", specialization="Paediatrics"),
        _entity("s1", "service", "ECG", category="Diagnostics", fee=300, currency="INR"),
    ],
    "relations": [
        {
            "id": "r1",
            "relation_type": "doctor_offers_service",
            "from_entity_id": "d1",
            "to_entity_id": "s1",
            "attributes": {"fee": 500, "currency": "INR"},
        },
    ],
    "availability": {
        "rules": [
            {
                "id": "h1",
                "entity_id": "d1",
                "location_entity_id": None,
                "timezone": "Asia/Kolkata",
                "rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
                "start_time": "10:00",
                "end_time": "14:00",
            },
        ],
        "exceptions": [
            {
                "id": "x1",
                "entity_id": "d1",
                "location_entity_id": None,
                "timezone": "Asia/Kolkata",
                "date": "2026-10-21",
                "is_available": False,
                "start_time": None,
                "end_time": None,
                "public_message": "On leave",
            },
        ],
    },
    "work_item_kinds": [],
    "faqs": [
        {
            "id": "f1",
            "question": "Do you accept UPI?",
            "phrasings": ["Can I pay by UPI?"],
            "answer": "Yes, we accept UPI and cards.",
            "category": None,
            "entity_id": None,
        },
    ],
    "knowledge_sections": [
        {
            "id": "k1",
            "document_title": "Patient guide",
            "category": "about",
            "heading": "Timings",
            "text": "Open Monday to Saturday. The clinic is closed on Sundays.",
            "entity_id": None,
            "keywords": [],
        },
    ],
    "announcements": [
        {
            "id": "n1",
            "kind": "closure",
            "message": "Closed on Friday for Diwali.",
            "entity_id": None,
            "location_entity_id": None,
            "priority": 100,
            "starts_at": "2026-10-23T00:00:00+05:30",
            "ends_at": "2026-10-24T00:00:00+05:30",
        },
        {
            "id": "n2",
            "kind": "information",
            "message": "Old notice.",
            "entity_id": None,
            "location_entity_id": None,
            "priority": 100,
            "starts_at": "2026-10-01T00:00:00+05:30",
            "ends_at": "2026-10-02T00:00:00+05:30",
        },
    ],
}
SNAPSHOT = AgentSnapshot.model_validate(RAW)


def test_find_and_describe_entries():
    found = lookup.find_entities(SNAPSHOT, "heart specialist cardiology")
    assert [e["name"] for e in found["entities"]] == ["Dr. Asha Sharma"]
    assert lookup.find_entities(SNAPSHOT, "orthopaedics")["status"] == "not_found"
    detail = lookup.get_entity(SNAPSHOT, "Dr Sharma")  # titles and punctuation don't matter
    assert detail["entity"]["specialization"] == "Cardiology"
    assert detail["links"] == [
        {
            "relation": "doctor_offers_service",
            "with": "ECG",
            "with_type": "service",
            "fee": 500,
            "currency": "INR",
        }
    ]
    assert lookup.get_entity(SNAPSHOT, "Dr")["status"] == "not_found"  # ambiguous


def test_availability_applies_rules_and_exceptions():
    monday = lookup.get_availability(SNAPSHOT, "Asha Sharma", NOW, "2026-10-19")
    assert monday["days"] == [
        {"date": "Monday 2026-10-19", "available": True, "hours": ["10:00-14:00"], "note": None}
    ]
    assert (
        lookup.get_availability(SNAPSHOT, "Sharma", NOW, "2026-10-20")["days"][0]["available"]
        is False
    )
    leave = lookup.get_availability(SNAPSHOT, "Sharma", NOW, "2026-10-21")["days"][0]
    assert (leave["available"], leave["note"]) == (False, "On leave")
    week = lookup.get_availability(SNAPSHOT, "Sharma", NOW)
    assert len(week["days"]) == 7 and week["today"] == "2026-10-19"
    assert lookup.get_availability(SNAPSHOT, "Ravi Menon", NOW)["status"] == "no_published_hours"
    assert (
        lookup.get_availability(SNAPSHOT, "Sharma", NOW, "next friday")["status"] == "invalid_date"
    )


def test_search_and_live_updates():
    sunday = lookup.search_knowledge(SNAPSHOT, "Are you open on Sunday?", NOW)["passages"]
    assert sunday[0]["heading"] == "Timings"
    upi = lookup.search_knowledge(SNAPSHOT, "can I pay with UPI", NOW)["passages"]
    assert upi[0]["source"] == "approved answer"
    diwali = lookup.search_knowledge(SNAPSHOT, "closed Diwali", NOW)["passages"]
    assert diwali[0]["source"] == "Scheduled live update"
    updates = lookup.get_announcements(SNAPSHOT, NOW)["updates"]
    assert [(u["state"], u["message"]) for u in updates] == [
        ("scheduled", "Closed on Friday for Diwali.")
    ]  # expired notices are gone


def test_prompt_uses_the_release():
    prompt = render_release_prompt(SNAPSHOT, NOW)
    assert "Sunrise Reception" in prompt and "Sunrise Clinic" in prompt
    assert "Please call 112 now." in prompt and "Our team will call you back." in prompt
    assert "Monday" in prompt and "2 doctors, 1 service" in prompt
    assert "get_availability" in prompt and "get_entity" not in prompt  # only enabled tools
    assert "Closed on Friday for Diwali." in prompt and "Old notice" not in prompt


def test_loader_refuses_anything_but_a_valid_v4_release():
    assert interpret({"reason": "no_live_release"}) == NoRelease("no_live_release")
    assert interpret(None) == NoRelease("database_unavailable")
    old = {
        "release_id": str(uuid.uuid4()),
        "version_no": 1,
        "workspace_id": str(uuid.uuid4()),
        "agent_id": str(uuid.uuid4()),
        "snapshot": RAW | {"schema_version": 3},
    }
    assert interpret(old) == NoRelease("unsupported_schema_version")
    broken = old | {"snapshot": RAW | {"entities": [{"id": "x"}]}}
    assert interpret(broken) == NoRelease("invalid_snapshot")
    good = interpret(old | {"snapshot": RAW, "version_no": 3})
    assert isinstance(good, LoadedRelease) and good.version_no == 3
    assert good.snapshot.agent.greeting.startswith("Namaste")
