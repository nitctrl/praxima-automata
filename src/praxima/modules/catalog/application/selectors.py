"""Reads for the catalog. RLS limits every query to the scoped workspace."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from praxima.modules import tenancy
from praxima.modules.catalog.infrastructure.models import (
    AvailabilityException,
    AvailabilityRule,
    Entity,
    EntityRelation,
    EntityType,
)
from praxima.shared.db.pagination import PageRequest, PageResult, fetch_page
from praxima.shared.errors import NotFound


@dataclass(frozen=True)
class EntityTypeView:
    id: uuid.UUID
    key: str
    name: str
    description: str | None
    schema_version: int
    attributes_schema: dict[str, Any]
    searchable_fields: list[str]
    display_template: dict[str, Any] | None


@dataclass(frozen=True)
class EntityView:
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


@dataclass(frozen=True)
class RelationView:
    id: uuid.UUID
    relation_type: str
    direction: str  # "outgoing" (this entity is `from`) or "incoming"
    other_id: uuid.UUID
    other_key: str
    other_name: str
    other_type: str
    attributes: dict[str, Any] | None
    publication_status: str


@dataclass(frozen=True)
class RuleView:
    id: uuid.UUID
    entity_id: uuid.UUID | None
    location_entity_id: uuid.UUID | None
    timezone: str
    rrule: str
    start_time: time
    end_time: time
    publication_status: str


@dataclass(frozen=True)
class ExceptionView:
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


@dataclass(frozen=True)
class RelationTypeView:
    key: str
    from_type: str
    to_type: str
    description: str
    attributes_schema: dict[str, Any]


async def relation_types(session: AsyncSession, workspace_id: uuid.UUID) -> list[RelationTypeView]:
    """The link types the workspace's pack allows (e.g. doctor offers service, with a fee)."""
    pack = await tenancy.installed_pack(session, workspace_id)
    return [
        RelationTypeView(r.key, r.from_type, r.to_type, r.description, r.attributes_schema)
        for r in pack.relation_types
    ]


async def entity_types(session: AsyncSession) -> list[EntityTypeView]:
    """The newest active version of every entity type (from the pack, plus custom)."""
    rows = await session.scalars(
        select(EntityType)
        .where(EntityType.status == "active")
        .distinct(EntityType.key)
        .order_by(EntityType.key, EntityType.schema_version.desc())
    )
    return [
        EntityTypeView(
            t.id,
            t.key,
            t.name,
            t.description,
            t.schema_version,
            t.attributes_schema,
            list(t.searchable_fields),
            t.display_template,
        )
        for t in rows
    ]


def _entity_view(entity: Entity, type_key: str) -> EntityView:
    return EntityView(
        entity.id,
        type_key,
        entity.key,
        entity.name,
        list(entity.aliases),
        entity.attributes,
        entity.publication_status,
        entity.row_version,
        entity.created_at,
        entity.updated_at,
    )


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def entities_page(
    session: AsyncSession,
    page: PageRequest,
    *,
    type_key: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> PageResult:
    """Newest first. The type key comes from a join in the same query (no N+1)."""
    conditions: list[ColumnElement[bool]] = [Entity.deleted_at.is_(None)]
    if type_key is not None:
        conditions.append(EntityType.key == type_key)
    if status is not None:
        conditions.append(Entity.publication_status == status)
    if search:
        conditions.append(
            or_(
                Entity.name.ilike(_like(search)),
                func.array_to_string(Entity.aliases, " ").ilike(_like(search)),
            )
        )
    statement = (
        select(Entity, EntityType.key.label("type_key"))
        .join(EntityType, EntityType.id == Entity.entity_type_id)
        .where(and_(*conditions))
    )
    result = await fetch_page(session, statement, (Entity.created_at, Entity.id), page)
    return PageResult(
        [_entity_view(row.Entity, row.type_key) for row in result.items], result.next_cursor
    )


async def get_entity(session: AsyncSession, entity_id: uuid.UUID) -> EntityView:
    row = (
        await session.execute(
            select(Entity, EntityType.key.label("type_key"))
            .join(EntityType, EntityType.id == Entity.entity_type_id)
            .where(Entity.id == entity_id, Entity.deleted_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Item not found.")
    return _entity_view(row.Entity, row.type_key)


async def relations_of(session: AsyncSession, entity_id: uuid.UUID) -> list[RelationView]:
    """Both directions, with the other side's name and type, in one query."""
    other = aliased(Entity)
    other_type = aliased(EntityType)
    outgoing = EntityRelation.from_entity_id == entity_id
    incoming = EntityRelation.to_entity_id == entity_id
    rows = await session.execute(
        select(
            EntityRelation,
            other.id.label("other_id"),
            other.key.label("other_key"),
            other.name.label("other_name"),
            other_type.key.label("other_type"),
            outgoing.label("outgoing"),
        )
        .join(
            other,
            or_(
                and_(outgoing, other.id == EntityRelation.to_entity_id),
                and_(incoming, other.id == EntityRelation.from_entity_id),
            ),
        )
        .join(other_type, other_type.id == other.entity_type_id)
        .where(
            or_(outgoing, incoming),
            EntityRelation.deleted_at.is_(None),
            other.deleted_at.is_(None),
        )
        .order_by(EntityRelation.relation_type, other.name)
    )
    return [
        RelationView(
            r.EntityRelation.id,
            r.EntityRelation.relation_type,
            "outgoing" if r.outgoing else "incoming",
            r.other_id,
            r.other_key,
            r.other_name,
            r.other_type,
            r.EntityRelation.attributes,
            r.EntityRelation.publication_status,
        )
        for r in rows
    ]


async def availability_of(
    session: AsyncSession, entity_id: uuid.UUID
) -> tuple[list[RuleView], list[ExceptionView]]:
    """Weekly rules and dated exceptions for an entity (as subject or as location)."""
    subject = or_(
        AvailabilityRule.entity_id == entity_id, AvailabilityRule.location_entity_id == entity_id
    )
    rules = await session.scalars(
        select(AvailabilityRule)
        .where(subject, AvailabilityRule.deleted_at.is_(None))
        .order_by(AvailabilityRule.start_time, AvailabilityRule.id)
    )
    exceptions = await session.scalars(
        select(AvailabilityException)
        .where(
            or_(
                AvailabilityException.entity_id == entity_id,
                AvailabilityException.location_entity_id == entity_id,
            ),
            AvailabilityException.deleted_at.is_(None),
        )
        .order_by(AvailabilityException.exception_date, AvailabilityException.id)
    )
    return (
        [
            RuleView(
                r.id,
                r.entity_id,
                r.location_entity_id,
                r.timezone,
                r.rrule,
                r.start_time,
                r.end_time,
                r.publication_status,
            )
            for r in rules
        ],
        [
            ExceptionView(
                e.id,
                e.entity_id,
                e.location_entity_id,
                e.timezone,
                e.exception_date,
                e.is_available,
                e.start_time,
                e.end_time,
                e.public_message,
                e.publication_status,
            )
            for e in exceptions
        ],
    )


@dataclass(frozen=True)
class PublishedCatalog:
    """Everything published in the directory, as plain JSON-ready data in a stable order.

    Feeds agent release snapshots, so the same content must always serialize identically.
    """

    entity_types: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    exceptions: list[dict[str, Any]]


def _valid_now(column: Any, at: datetime) -> ColumnElement[bool]:
    return or_(column.is_(None), column.op("@>")(at))


def _time(value: time | None) -> str | None:
    return value.isoformat(timespec="minutes") if value else None


async def published_catalog(session: AsyncSession, at: datetime) -> PublishedCatalog:
    """Published entities valid at `at`, their published links and hours (five queries).

    Links, hours and exceptions are kept only when every entity they mention is published;
    exceptions dated before `at` are dropped. Validity windows are applied here; the runtime
    applies hours and exceptions against the call's own clock.
    """
    types = list(
        await session.scalars(
            select(EntityType)
            .where(EntityType.status == "active", EntityType.deleted_at.is_(None))
            .order_by(EntityType.key)
        )
    )
    type_keys = {t.id: t.key for t in types}
    entities = list(
        await session.scalars(
            select(Entity)
            .where(
                Entity.publication_status == "published",
                Entity.deleted_at.is_(None),
                Entity.entity_type_id.in_(type_keys),
                _valid_now(Entity.valid_during, at),
            )
            .order_by(Entity.key, Entity.id)
        )
    )
    live = {e.id for e in entities}

    def known(*ids: uuid.UUID | None) -> bool:
        return all(i is None or i in live for i in ids)

    relations = await session.scalars(
        select(EntityRelation)
        .where(
            EntityRelation.publication_status == "published",
            EntityRelation.deleted_at.is_(None),
            _valid_now(EntityRelation.valid_during, at),
        )
        .order_by(EntityRelation.relation_type, EntityRelation.id)
    )
    rules = await session.scalars(
        select(AvailabilityRule)
        .where(
            AvailabilityRule.publication_status == "published",
            AvailabilityRule.deleted_at.is_(None),
            _valid_now(AvailabilityRule.valid_during, at),
        )
        .order_by(AvailabilityRule.id)
    )
    exceptions = await session.scalars(
        select(AvailabilityException)
        .where(
            AvailabilityException.publication_status == "published",
            AvailabilityException.deleted_at.is_(None),
            AvailabilityException.exception_date >= at.date(),
        )
        .order_by(AvailabilityException.exception_date, AvailabilityException.id)
    )
    return PublishedCatalog(
        entity_types=[
            {
                "key": t.key,
                "name": t.name,
                "schema_version": t.schema_version,
                "searchable_fields": list(t.searchable_fields),
            }
            for t in types
        ],
        entities=[
            {
                "id": str(e.id),
                "type": type_keys[e.entity_type_id],
                "key": e.key,
                "name": e.name,
                "aliases": list(e.aliases),
                "attributes": e.attributes,
            }
            for e in entities
        ],
        relations=[
            {
                "id": str(r.id),
                "relation_type": r.relation_type,
                "from_entity_id": str(r.from_entity_id),
                "to_entity_id": str(r.to_entity_id),
                "attributes": r.attributes or {},
            }
            for r in relations
            if known(r.from_entity_id, r.to_entity_id)
        ],
        rules=[
            {
                "id": str(r.id),
                "entity_id": str(r.entity_id) if r.entity_id else None,
                "location_entity_id": str(r.location_entity_id) if r.location_entity_id else None,
                "timezone": r.timezone,
                "rrule": r.rrule,
                "start_time": _time(r.start_time),
                "end_time": _time(r.end_time),
            }
            for r in rules
            if known(r.entity_id, r.location_entity_id)
        ],
        exceptions=[
            {
                "id": str(x.id),
                "entity_id": str(x.entity_id) if x.entity_id else None,
                "location_entity_id": str(x.location_entity_id) if x.location_entity_id else None,
                "timezone": x.timezone,
                "date": x.exception_date.isoformat(),
                "is_available": x.is_available,
                "start_time": _time(x.start_time),
                "end_time": _time(x.end_time),
                "public_message": x.public_message,
            }
            for x in exceptions
            if known(x.entity_id, x.location_entity_id)
        ],
    )


async def entity_names(session: AsyncSession, entity_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Display name of each entity, in one query (deleted ones included, for history)."""
    if not entity_ids:
        return {}
    rows = await session.execute(
        select(Entity.id, Entity.name).where(Entity.id.in_(set(entity_ids)))
    )
    return dict(rows.tuples().all())
