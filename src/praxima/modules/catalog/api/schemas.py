"""Request and response models for catalog endpoints."""

import uuid
from datetime import date, datetime, time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from praxima.modules import catalog

Status = Literal["draft", "published", "archived"]
EntityKey = Field(min_length=1, max_length=150, pattern=r"^[a-z0-9][a-z0-9-]{0,149}$")
Alias = Annotated[str, Field(min_length=1, max_length=200)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityTypeOut(BaseModel):
    key: str
    name: str
    description: str | None
    schema_version: int
    attributes_schema: dict[str, Any]
    searchable_fields: list[str]
    display_template: dict[str, Any] | None

    @classmethod
    def of(cls, view: catalog.EntityTypeView) -> "EntityTypeOut":
        values = {k: v for k, v in view.__dict__.items() if k != "id"}
        return cls(**values)


class EntityOut(BaseModel):
    id: uuid.UUID
    type: str
    key: str
    name: str
    aliases: list[str]
    attributes: dict[str, Any]
    publication_status: str
    row_version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: catalog.EntityView) -> "EntityOut":
        return cls(**view.__dict__)


class EntityIn(Strict):
    type: str = Field(min_length=2, max_length=63)
    key: str = EntityKey
    name: str = Field(min_length=1, max_length=200)
    aliases: list[Alias] = Field(default=[], max_length=20)
    attributes: dict[str, Any] = {}

    def draft(self) -> catalog.EntityDraft:
        return catalog.EntityDraft(
            self.type, self.key, self.name, tuple(self.aliases), self.attributes
        )


class EntityPatch(Strict):
    """Partial update. `row_version` must be the value last read (409 if it changed)."""

    row_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[Alias] | None = Field(default=None, max_length=20)
    attributes: dict[str, Any] | None = None
    publication_status: Status | None = None

    def changes(self) -> catalog.EntityChanges:
        return catalog.EntityChanges(
            name=self.name,
            aliases=tuple(self.aliases) if self.aliases is not None else None,
            attributes=self.attributes,
        )


class RelationIn(Strict):
    relation_type: str = Field(min_length=2, max_length=63)
    from_entity_id: uuid.UUID
    to_entity_id: uuid.UUID
    attributes: dict[str, Any] | None = None


class RelationOut(BaseModel):
    id: uuid.UUID
    relation_type: str
    direction: str
    other_id: uuid.UUID
    other_key: str
    other_name: str
    other_type: str
    attributes: dict[str, Any] | None
    publication_status: str

    @classmethod
    def of(cls, view: catalog.RelationView) -> "RelationOut":
        return cls(**view.__dict__)


class StatusIn(Strict):
    publication_status: Status


class CreatedOut(BaseModel):
    id: uuid.UUID


class RuleIn(Strict):
    timezone: str = Field(min_length=1, max_length=64)
    rrule: str = Field(min_length=5, max_length=500)
    start_time: time
    end_time: time
    entity_id: uuid.UUID | None = None
    location_entity_id: uuid.UUID | None = None

    def draft(self) -> catalog.AvailabilityRuleDraft:
        return catalog.AvailabilityRuleDraft(**self.model_dump())


class ExceptionIn(Strict):
    timezone: str = Field(min_length=1, max_length=64)
    exception_date: date
    is_available: bool
    entity_id: uuid.UUID | None = None
    location_entity_id: uuid.UUID | None = None
    availability_rule_id: uuid.UUID | None = None
    start_time: time | None = None
    end_time: time | None = None
    public_message: str | None = Field(default=None, max_length=500)

    def draft(self) -> catalog.AvailabilityExceptionDraft:
        return catalog.AvailabilityExceptionDraft(**self.model_dump())


class RuleOut(BaseModel):
    id: uuid.UUID
    entity_id: uuid.UUID | None
    location_entity_id: uuid.UUID | None
    timezone: str
    rrule: str
    start_time: time
    end_time: time
    publication_status: str


class ExceptionOut(BaseModel):
    id: uuid.UUID
    entity_id: uuid.UUID | None
    location_entity_id: uuid.UUID | None
    timezone: str
    exception_date: date
    is_available: bool
    start_time: time | None
    end_time: time | None
    public_message: str | None
    publication_status: str


class AvailabilityOut(BaseModel):
    rules: list[RuleOut]
    exceptions: list[ExceptionOut]
