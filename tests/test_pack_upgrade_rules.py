"""Which newer pack versions a workspace with data may move to (no database)."""

from typing import Any

from praxima.modules.tenancy.application.selectors import upgrade_problems, version_key
from praxima.packs import loader
from praxima.packs.loader import Pack

CLINIC = loader.load("clinic")


def changed(**edits: Any) -> Pack:
    """The clinic pack as version 9.0.0, with `edits` applied to its payload."""
    payload = CLINIC.payload() | {"version": "9.0.0"}
    for name, edit in edits.items():
        payload[name] = edit(payload[name])
    return Pack.from_payload(payload)


def with_type(types: list[dict[str, Any]], key: str, **fields: Any) -> list[dict[str, Any]]:
    return [t | fields if t["key"] == key else t for t in types]


def test_versions_compare_numerically():
    assert version_key("1.10.0") > version_key("1.9.3") > version_key("1.9.0")


def test_additions_and_wording_are_safe():
    added = changed(
        entity_types=lambda types: [*types, types[0] | {"key": "lab_test", "name": "Lab test"}]
    )
    renamed = changed(entity_types=lambda types: with_type(types, "doctor", name="Physician"))
    assert upgrade_problems(CLINIC, added) == []
    assert upgrade_problems(CLINIC, renamed) == []


def test_new_fields_need_a_new_schema_version():
    doctor = next(t for t in CLINIC.payload()["entity_types"] if t["key"] == "doctor")
    fields = doctor["attributes_schema"] | {"required": ["specialization", "room"]}
    same = changed(entity_types=lambda types: with_type(types, "doctor", attributes_schema=fields))
    bumped = changed(
        entity_types=lambda types: with_type(
            types, "doctor", attributes_schema=fields, schema_version=doctor["schema_version"] + 1
        )
    )
    assert upgrade_problems(CLINIC, same) == [
        "Changes the 'doctor' fields without a new schema_version."
    ]
    assert upgrade_problems(CLINIC, bumped) == []


def test_removing_what_data_uses_is_refused():
    kind = CLINIC.work_item_kinds[0].key
    fewer = changed(work_item_kinds=lambda kinds: [k for k in kinds if k["key"] != kind])
    assert upgrade_problems(CLINIC, fewer) == [f"Removes the request type '{kind}'."]
    stages = changed(
        work_item_kinds=lambda kinds: [
            k | {"stages": [*k["stages"], "archived"]} if k["key"] == kind else k for k in kinds
        ]
    )
    assert upgrade_problems(CLINIC, stages) == [
        f"Changes the '{kind}' request without a new schema_version."
    ]
