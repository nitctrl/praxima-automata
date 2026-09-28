"""The agent release snapshot (schema version 4): everything an agent may use on a call.

Built only from published content. It is validated here before it is stored, and its digest is
the SHA-256 of its canonical JSON, so the same content always gives the same digest. The
voice runtime must refuse any `schema_version` it doesn't understand.
"""

import hashlib
import json
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal[4] = 4
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackRef(_Strict):
    key: str
    version: str


class WorkspaceInfo(_Strict):
    name: str
    timezone: str
    default_language: str
    supported_languages: list[str]


class AgentInfo(_Strict):
    id: str
    name: str
    persona: str | None
    greeting: str
    emergency_message: str
    fallback_message: str
    transfer_enabled: bool
    prompt_version: int = Field(ge=1)


class Tool(_Strict):
    key: str


class EntityTypeItem(_Strict):
    key: str
    name: str
    schema_version: int
    searchable_fields: list[str]


class EntityItem(_Strict):
    id: str
    type: str
    key: str
    name: str
    aliases: list[str]
    attributes: dict[str, Any]


class RelationItem(_Strict):
    id: str
    relation_type: str
    from_entity_id: str
    to_entity_id: str
    attributes: dict[str, Any]


class RuleItem(_Strict):
    id: str
    entity_id: str | None
    location_entity_id: str | None
    timezone: str
    rrule: str
    start_time: str | None
    end_time: str | None


class ExceptionItem(_Strict):
    id: str
    entity_id: str | None
    location_entity_id: str | None
    timezone: str
    date: str
    is_available: bool
    start_time: str | None
    end_time: str | None
    public_message: str | None


class Availability(_Strict):
    rules: list[RuleItem]
    exceptions: list[ExceptionItem]


class WorkItemKindItem(_Strict):
    key: str
    name: str
    schema_version: int
    payload_schema: dict[str, Any]
    stages: list[str]
    initial_stage: str
    terminal_stages: list[str]
    subject_types: list[str]


class FaqItem(_Strict):
    id: str
    question: str
    phrasings: list[str]
    answer: str
    category: str | None
    entity_id: str | None


class SectionItem(_Strict):
    id: str
    document_title: str
    category: str | None
    heading: str | None
    text: str
    entity_id: str | None
    keywords: list[str]


class AnnouncementItem(_Strict):
    id: str
    kind: str
    message: str
    entity_id: str | None
    location_entity_id: str | None
    priority: int
    starts_at: str | None
    ends_at: str | None


class AgentSnapshot(_Strict):
    schema_version: Literal[4] = SCHEMA_VERSION
    pack: PackRef
    workspace: WorkspaceInfo
    agent: AgentInfo
    tools: list[Tool]
    entity_types: list[EntityTypeItem]
    entities: list[EntityItem]
    relations: list[RelationItem]
    availability: Availability
    work_item_kinds: list[WorkItemKindItem]
    faqs: list[FaqItem]
    knowledge_sections: list[SectionItem]
    announcements: list[AnnouncementItem]


def canonical_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(snapshot: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()


def summarize(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Item counts per section, for release lists and the preview screen."""
    return {
        "entities": dict(sorted(Counter(e["type"] for e in snapshot["entities"]).items())),
        "relations": len(snapshot["relations"]),
        "hours": len(snapshot["availability"]["rules"]),
        "exceptions": len(snapshot["availability"]["exceptions"]),
        "documents": len({s["document_title"] for s in snapshot["knowledge_sections"]}),
        "sections": len(snapshot["knowledge_sections"]),
        "faqs": len(snapshot["faqs"]),
        "announcements": len(snapshot["announcements"]),
        "work_item_kinds": len(snapshot["work_item_kinds"]),
        "tools": len(snapshot["tools"]),
    }


# Section → (path to the list, identity key of an item).
_LISTS: dict[str, tuple[tuple[str, ...], str]] = {
    "entities": (("entities",), "id"),
    "relations": (("relations",), "id"),
    "hours": (("availability", "rules"), "id"),
    "exceptions": (("availability", "exceptions"), "id"),
    "sections": (("knowledge_sections",), "id"),
    "faqs": (("faqs",), "id"),
    "announcements": (("announcements",), "id"),
    "work_item_kinds": (("work_item_kinds",), "key"),
    "tools": (("tools",), "key"),
}
_SETTINGS = ("pack", "workspace", "agent", "entity_types")


def _items(snapshot: dict[str, Any], path: tuple[str, ...], key: str) -> dict[str, str]:
    value: Any = snapshot
    for part in path:
        value = value[part]
    return {str(item[key]): canonical_json(item) for item in value}


def diff(live: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """What publishing `new` would change compared with the live snapshot."""
    sections: dict[str, dict[str, int]] = {}
    for name, (path, key) in _LISTS.items():
        after = _items(new, path, key)
        before = _items(live, path, key) if live else {}
        change = {
            "added": len(after.keys() - before.keys()),
            "removed": len(before.keys() - after.keys()),
            "changed": sum(1 for k in after.keys() & before.keys() if after[k] != before[k]),
        }
        if any(change.values()):
            sections[name] = change
    settings = [
        name
        for name in _SETTINGS
        if live is None or canonical_json(live[name]) != canonical_json(new[name])
    ]
    return {"sections": sections, "settings": settings}
