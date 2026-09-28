"""Domain pack loading and validation (no database)."""

import copy

import pytest

from praxima.modules.catalog.domain.rules import attribute_errors
from praxima.packs import loader
from praxima.packs.loader import Pack, PackError

CLINIC = loader.load("clinic")


def broken(change) -> dict:  # type: ignore[no-untyped-def, type-arg]
    payload = copy.deepcopy(CLINIC.payload())
    change(payload)
    return payload


def test_clinic_pack_is_valid_and_stable():
    assert loader.available() == ["clinic", "real_estate"]
    assert [t.key for t in CLINIC.entity_types] == ["doctor", "service", "location"]
    assert CLINIC.relation_type("doctor_offers_service").to_type == "service"
    assert Pack.from_payload(CLINIC.payload()).checksum() == CLINIC.checksum()
    assert set(CLINIC.tools) <= loader.GENERIC_TOOLS


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p["tools"].append("book_appointment"), "unknown tools"),
        (lambda p: p["relation_types"][0].update({"from": "nurse"}), "unknown entity type"),
        (lambda p: p.update({"availability_for": ["service", "room"]}), "unknown entity type"),
        (
            lambda p: p["entity_types"][0]["attributes_schema"].update({"type": "array"}),
            "JSON object schema",
        ),
        (
            lambda p: p["entity_types"][0]["attributes_schema"].update({"required": "x"}),
            "invalid JSON Schema",
        ),
        (lambda p: p["entity_types"][0].update({"searchable_fields": ["fee"]}), "searchable"),
        (lambda p: p["entity_types"].append(p["entity_types"][0]), "duplicate entity types"),
        (lambda p: p.update({"version": "1.0"}), "version"),
    ],
)
def test_invalid_packs_are_rejected(change, message):
    with pytest.raises(PackError, match=message):
        Pack.from_payload(broken(change))


def test_attribute_errors_never_echo_values():
    schema = CLINIC.entity_type("doctor").attributes_schema
    errors = attribute_errors(
        schema, {"experience_years": "secret-value", "national_id": "123-45", "languages": ["x y"]}
    )
    assert [(e.field, e.message) for e in errors] == [
        ("attributes.experience_years", "has the wrong type"),
        ("attributes.languages.0", "has an invalid format"),
        ("attributes.national_id", "is not a known field"),
        ("attributes.specialization", "is required"),
    ]
    assert "secret" not in repr(errors) and "123-45" not in repr(errors)
    assert attribute_errors(schema, {"specialization": "Cardiology"}) == []


@pytest.mark.parametrize("key", loader.available())
def test_every_pack_is_complete(key):
    """Pack parity: each shipped pack carries everything core code needs, as data."""
    from pathlib import Path

    pack = loader.load(key)
    assert Pack.from_payload(pack.payload()).checksum() == pack.checksum()
    assert set(pack.tools) <= loader.GENERIC_TOOLS
    assert pack.callback_kind in {k.key for k in pack.work_item_kinds}
    assert pack.agent_defaults is not None
    assert all(t.plural_name for t in pack.entity_types)
    assert pack.document_categories and pack.announcement_kinds
    prompt = Path(loader.PACKS_DIR, key, "prompts", "release_system_prompt.j2")
    assert prompt.is_file(), "each pack ships its own voice prompt"


@pytest.mark.parametrize("key", loader.available())
def test_every_pack_renders_its_own_prompt(key):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from praxima.modules.releases.domain.agent_snapshot import AgentSnapshot
    from praxima.runtime.prompting import render_release_prompt

    pack = loader.load(key)
    snapshot = AgentSnapshot.model_validate(
        {
            "pack": {"key": key, "version": pack.version, "callback_kind": pack.callback_kind},
            "workspace": {
                "name": "Acme",
                "timezone": "Asia/Kolkata",
                "default_language": "hi-IN",
                "supported_languages": list(pack.languages),
            },
            "agent": {
                "id": "a1",
                "name": "Desk",
                "persona": None,
                "greeting": pack.agent_defaults.greeting_message,  # type: ignore[union-attr]
                "emergency_message": pack.agent_defaults.emergency_message,  # type: ignore[union-attr]
                "fallback_message": pack.agent_defaults.fallback_message,  # type: ignore[union-attr]
                "transfer_enabled": False,
                "prompt_version": 1,
            },
            "tools": [{"key": t} for t in pack.tools],
            "entity_types": [
                {
                    "key": t.key,
                    "name": t.name,
                    "schema_version": t.schema_version,
                    "searchable_fields": list(t.searchable_fields),
                }
                for t in pack.entity_types
            ],
            "entities": [],
            "relations": [],
            "availability": {"rules": [], "exceptions": []},
            "work_item_kinds": [
                {
                    "key": k.key,
                    "name": k.name,
                    "schema_version": k.schema_version,
                    "payload_schema": k.payload_schema,
                    "stages": list(k.stages),
                    "initial_stage": k.initial_stage,
                    "terminal_stages": list(k.terminal_stages),
                    "subject_types": list(k.subject_types),
                }
                for k in pack.work_item_kinds
            ],
            "faqs": [],
            "knowledge_sections": [],
            "announcements": [],
        }
    )
    prompt = render_release_prompt(
        snapshot, datetime(2026, 10, 19, 11, tzinfo=ZoneInfo("Asia/Kolkata"))
    )
    for kind in pack.work_item_kinds:
        assert kind.key in prompt  # every request type is offered, with its fields
    other_words = {
        "clinic": ["property", "possession", "RERA"],
        "real_estate": ["doctor", "diagnose", "medicine"],
    }
    for word in other_words.get(key, []):
        assert word.lower() not in prompt.lower(), f"{key} prompt mentions {word!r}"
