"""Agent release snapshot rules, without a database."""

import copy

import pytest
from pydantic import ValidationError

from praxima.modules.releases.domain.agent_snapshot import (
    AgentSnapshot,
    diff,
    digest_of,
    summarize,
)

SNAPSHOT = {
    "schema_version": 4,
    "pack": {"key": "clinic", "version": "1.0.0"},
    "workspace": {
        "name": "Sunrise",
        "timezone": "Asia/Kolkata",
        "default_language": "hi-IN",
        "supported_languages": ["hi-IN", "en-IN"],
    },
    "agent": {
        "id": "a1",
        "name": "Desk",
        "persona": None,
        "greeting": "Namaste",
        "emergency_message": "Call 112",
        "fallback_message": "We'll call you back",
        "transfer_enabled": False,
        "prompt_version": 1,
    },
    "tools": [{"key": "find_entities"}],
    "entity_types": [],
    "entities": [
        {
            "id": "e1",
            "type": "doctor",
            "key": "dr-a",
            "name": "Dr A",
            "aliases": [],
            "attributes": {},
        }
    ],
    "relations": [],
    "availability": {"rules": [], "exceptions": []},
    "work_item_kinds": [],
    "faqs": [],
    "knowledge_sections": [],
    "announcements": [],
}


def test_snapshot_is_strict_and_versioned():
    dumped = AgentSnapshot.model_validate(SNAPSHOT).model_dump(mode="json")
    # callback_kind was added compatibly: older snapshots validate, and dump it as None.
    assert dumped == SNAPSHOT | {"pack": SNAPSHOT["pack"] | {"callback_kind": None}}
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate(SNAPSHOT | {"schema_version": 3})
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate(SNAPSHOT | {"internal_notes": "secret"})


def test_digest_is_canonical():
    reordered = dict(reversed(list(SNAPSHOT.items())))
    assert digest_of(reordered) == digest_of(SNAPSHOT)
    assert digest_of(SNAPSHOT).startswith("sha256:") and len(digest_of(SNAPSHOT)) == 71
    changed = copy.deepcopy(SNAPSHOT)
    changed["agent"]["greeting"] = "Hello"
    assert digest_of(changed) != digest_of(SNAPSHOT)


def test_diff_and_summary():
    after = copy.deepcopy(SNAPSHOT)
    after["entities"][0]["name"] = "Dr A. Rao"
    after["entities"].append(
        {
            "id": "e2",
            "type": "service",
            "key": "ecg",
            "name": "ECG",
            "aliases": [],
            "attributes": {},
        }
    )
    after["tools"] = []
    change = diff(SNAPSHOT, after)
    assert change["sections"] == {
        "entities": {"added": 1, "removed": 0, "changed": 1},
        "tools": {"added": 0, "removed": 1, "changed": 0},
    }
    assert change["settings"] == []
    assert diff(None, SNAPSHOT)["settings"] == ["pack", "workspace", "agent", "entity_types"]
    assert summarize(after)["entities"] == {"doctor": 1, "service": 1}
