"""Domain-neutral business data: typed entities, relations and availability."""

import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, Range
from sqlalchemy.orm import Mapped, mapped_column, relationship

from praxima.shared.db.base import AuthoringMixin, Base, IdMixin, TenantMixin

SCHEMA = "catalog"
PUBLICATION = "publication_status IN ('draft', 'published', 'archived')"
KEY_FORMAT = "^[a-z0-9][a-z0-9-]{0,149}$"


def _fk(columns: list[str], table: str) -> ForeignKeyConstraint:
    """Composite tenant key: a row can only reference a row of the same workspace."""
    return ForeignKeyConstraint(
        ["workspace_id", *columns], [f"{SCHEMA}.{table}.workspace_id", f"{SCHEMA}.{table}.id"]
    )


class EntityType(IdMixin, TenantMixin, AuthoringMixin, Base):
    __tablename__ = "entity_types"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", "schema_version"),
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("key ~ '^[a-z][a-z0-9_]{1,62}$'", name="key_format"),
        CheckConstraint("source IN ('pack', 'custom')", name="source"),
        CheckConstraint("status IN ('active', 'deprecated')", name="status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.workspaces.id"))
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int]
    attributes_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    searchable_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    display_template: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    source: Mapped[str] = mapped_column(Text, default="pack", server_default=text("'pack'"))
    source_pack_key: Mapped[str | None] = mapped_column(Text)
    source_pack_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default=text("'active'"))


class Entity(IdMixin, TenantMixin, AuthoringMixin, Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _fk(["entity_type_id"], "entity_types"),
        # Keys are unique among live entities; a deleted key can be reused.
        Index(
            None,
            "workspace_id",
            "key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(None, "workspace_id", "entity_type_id", "publication_status"),
        # Fuzzy name search ("Dr Sharma" ~ "sharma"); opclass from pg_trgm (extensions schema).
        Index(
            "ix_entities_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        CheckConstraint(f"key ~ '{KEY_FORMAT}'", name="key_format"),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name="attributes_object"),
        CheckConstraint(PUBLICATION, name="publication_status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.workspaces.id"))
    entity_type_id: Mapped[uuid.UUID]
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'")
    )
    publication_status: Mapped[str] = mapped_column(
        Text, default="draft", server_default=text("'draft'")
    )
    valid_during: Mapped[Range[datetime] | None] = mapped_column(TSTZRANGE)

    entity_type: Mapped[EntityType] = relationship(
        lazy="raise",
        primaryjoin="and_(Entity.entity_type_id == EntityType.id, "
        "Entity.workspace_id == EntityType.workspace_id)",
        foreign_keys="[Entity.workspace_id, Entity.entity_type_id]",
        viewonly=True,
    )


class EntityRelation(IdMixin, TenantMixin, AuthoringMixin, Base):
    """E.g. doctor_offers_service. Overlapping live duplicates are refused (exclusion)."""

    __tablename__ = "entity_relations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _fk(["from_entity_id"], "entities"),
        _fk(["to_entity_id"], "entities"),
        Index(None, "workspace_id", "from_entity_id", "relation_type"),
        Index(None, "workspace_id", "to_entity_id"),
        CheckConstraint("from_entity_id <> to_entity_id", name="not_self"),
        CheckConstraint(
            "attributes IS NULL OR jsonb_typeof(attributes) = 'object'", name="attributes_object"
        ),
        CheckConstraint(PUBLICATION, name="publication_status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.workspaces.id"))
    from_entity_id: Mapped[uuid.UUID]
    to_entity_id: Mapped[uuid.UUID]
    relation_type: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    publication_status: Mapped[str] = mapped_column(
        Text, default="draft", server_default=text("'draft'")
    )
    valid_during: Mapped[Range[datetime] | None] = mapped_column(TSTZRANGE)


class AvailabilityRule(IdMixin, TenantMixin, AuthoringMixin, Base):
    """Weekly hours: an RFC 5545 RRULE for the days, plus a daily time window."""

    __tablename__ = "availability_rules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _fk(["entity_id"], "entities"),
        _fk(["location_entity_id"], "entities"),
        CheckConstraint(
            "entity_id IS NOT NULL OR location_entity_id IS NOT NULL", name="has_subject"
        ),
        CheckConstraint("end_time > start_time", name="window"),
        CheckConstraint(PUBLICATION, name="publication_status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.workspaces.id"))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    location_entity_id: Mapped[uuid.UUID | None]
    timezone: Mapped[str] = mapped_column(Text)
    rrule: Mapped[str] = mapped_column(Text)
    start_time: Mapped[time]
    end_time: Mapped[time]
    valid_during: Mapped[Range[datetime] | None] = mapped_column(TSTZRANGE)
    publication_status: Mapped[str] = mapped_column(
        Text, default="draft", server_default=text("'draft'")
    )


class AvailabilityException(IdMixin, TenantMixin, AuthoringMixin, Base):
    """A dated change: closed (is_available false) or special hours for that day."""

    __tablename__ = "availability_exceptions"
    __table_args__ = (
        _fk(["availability_rule_id"], "availability_rules"),
        _fk(["entity_id"], "entities"),
        _fk(["location_entity_id"], "entities"),
        CheckConstraint(
            "availability_rule_id IS NOT NULL OR entity_id IS NOT NULL "
            "OR location_entity_id IS NOT NULL",
            name="has_subject",
        ),
        CheckConstraint(
            "(start_time IS NULL AND end_time IS NULL) "
            "OR (start_time IS NOT NULL AND end_time IS NOT NULL AND end_time > start_time)",
            name="window",
        ),
        CheckConstraint(
            "public_message IS NULL OR length(public_message) <= 500", name="message_length"
        ),
        CheckConstraint(PUBLICATION, name="publication_status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.workspaces.id"))
    availability_rule_id: Mapped[uuid.UUID | None]
    entity_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    location_entity_id: Mapped[uuid.UUID | None]
    timezone: Mapped[str] = mapped_column(Text)
    exception_date: Mapped[date]
    is_available: Mapped[bool]
    start_time: Mapped[time | None]
    end_time: Mapped[time | None]
    public_message: Mapped[str | None] = mapped_column(Text)
    publication_status: Mapped[str] = mapped_column(
        Text, default="draft", server_default=text("'draft'")
    )
