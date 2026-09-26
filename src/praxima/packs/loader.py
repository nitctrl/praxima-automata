"""Load and validate domain packs. A pack is versioned data; this is its only reader."""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PACKS_DIR = Path(__file__).resolve().parent
KEY = r"^[a-z][a-z0-9_]{1,62}$"
SEMVER = r"^\d+\.\d+\.\d+$"

# Generic voice tools the runtime implements. Packs choose from these; they never add code.
GENERIC_TOOLS = frozenset(
    {
        "find_entities",
        "get_entity",
        "get_availability",
        "search_knowledge",
        "get_announcements",
        "create_work_item",
        "request_callback",
        "transfer_to_human",
    }
)


class PackError(ValueError):
    """A pack's files are invalid. Raised at load or publish time, never mid-request."""


def _check_schema(schema: dict[str, Any], where: str) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise PackError(f"{where}: invalid JSON Schema ({exc.message})") from None
    if schema.get("type") != "object":
        raise PackError(f"{where}: attributes must be a JSON object schema")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class EntityTypeSpec(Strict):
    key: str = Field(pattern=KEY)
    name: str = Field(min_length=1, max_length=150)
    description: str = ""
    schema_version: int = Field(ge=1)
    searchable_fields: tuple[str, ...] = ()
    display_template: dict[str, str] = {}
    attributes_schema: dict[str, Any]

    @model_validator(mode="after")
    def _valid(self) -> "EntityTypeSpec":
        _check_schema(self.attributes_schema, f"entity type {self.key}")
        known = {"name", *self.attributes_schema.get("properties", {})}
        if unknown := set(self.searchable_fields) - known:
            raise PackError(f"entity type {self.key}: unknown searchable fields {sorted(unknown)}")
        return self


class RelationTypeSpec(Strict):
    key: str = Field(pattern=KEY)
    from_type: str = Field(alias="from", pattern=KEY)
    to_type: str = Field(alias="to", pattern=KEY)
    description: str = ""
    attributes_schema: dict[str, Any] = {"type": "object"}

    @model_validator(mode="after")
    def _valid(self) -> "RelationTypeSpec":
        _check_schema(self.attributes_schema, f"relation type {self.key}")
        return self


class Pack(Strict):
    key: str = Field(pattern=KEY)
    version: str = Field(pattern=SEMVER)
    name: str = Field(min_length=1, max_length=100)
    industry: str = Field(min_length=1, max_length=100)
    languages: tuple[str, ...] = Field(min_length=1)
    entity_types: tuple[EntityTypeSpec, ...] = Field(min_length=1)
    relation_types: tuple[RelationTypeSpec, ...] = ()
    availability_for: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> "Pack":
        types = [t.key for t in self.entity_types]
        if len(set(types)) != len(types):
            raise PackError(f"pack {self.key}: duplicate entity types")
        relations = [r.key for r in self.relation_types]
        if len(set(relations)) != len(relations):
            raise PackError(f"pack {self.key}: duplicate relation types")
        for relation in self.relation_types:
            if {relation.from_type, relation.to_type} - set(types):
                raise PackError(f"relation {relation.key}: unknown entity type")
        if set(self.availability_for) - set(types):
            raise PackError(f"pack {self.key}: availability for an unknown entity type")
        if unknown := set(self.tools) - GENERIC_TOOLS:
            raise PackError(f"pack {self.key}: unknown tools {sorted(unknown)}")
        return self

    def entity_type(self, key: str) -> EntityTypeSpec | None:
        return next((t for t in self.entity_types if t.key == key), None)

    def relation_type(self, key: str) -> RelationTypeSpec | None:
        return next((r for r in self.relation_types if r.key == key), None)

    def payload(self) -> dict[str, Any]:
        """The full pack as stored in tenancy.pack_versions.manifest."""
        return self.model_dump(mode="json", by_alias=True)

    def checksum(self) -> str:
        canonical = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Pack":
        return _validated(payload, str(payload.get("key", "?")))


def _validated(data: dict[str, Any], key: str) -> Pack:
    try:
        return Pack.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "manifest"
        raise PackError(f"pack {key}: {where}: {first['msg']}") from None


def available() -> list[str]:
    """Shipped packs (folders with a manifest); `_template` and private folders excluded."""
    return sorted(
        p.name for p in PACKS_DIR.iterdir() if (p / "manifest.yaml").is_file() and p.name[0] != "_"
    )


def load(key: str, root: Path = PACKS_DIR) -> Pack:
    folder = root / key
    try:
        manifest = yaml.safe_load((folder / "manifest.yaml").read_text())
        if not isinstance(manifest, dict) or manifest.get("key") != key:
            raise PackError(f"pack {key}: manifest key must match its folder")
        names = manifest.get("entity_types", [])
        manifest["entity_types"] = [
            json.loads((folder / "entity_types" / f"{name}.json").read_text()) for name in names
        ]
        return _validated(manifest, key)
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise PackError(f"pack {key}: unreadable ({type(exc).__name__})") from None
