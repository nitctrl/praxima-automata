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
    assert loader.available() == ["clinic"]
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
