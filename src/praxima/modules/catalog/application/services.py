"""Writes for the catalog: pack install, entities, relations and availability."""

import uuid
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any
from zoneinfo import available_timezones

from dateutil.rrule import rrulestr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import audit, tenancy
from praxima.modules.catalog.domain.rules import attribute_errors
from praxima.modules.catalog.infrastructure.models import (
    AvailabilityException,
    AvailabilityRule,
    Entity,
    EntityRelation,
    EntityType,
)
from praxima.modules.iam import Actor, require
from praxima.shared.db.base import utc_now
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import Conflict, FieldError, NotFound, ValidationFailed

_TIMEZONES = frozenset(available_timezones())
PUBLICATION_STATES = frozenset({"draft", "published", "archived"})


@dataclass(frozen=True)
class EntityDraft:
    type_key: str
    key: str
    name: str
    aliases: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityChanges:
    """PATCH fields; None means "leave unchanged"."""

    name: str | None = None
    aliases: tuple[str, ...] | None = None
    attributes: dict[str, Any] | None = None


@dataclass(frozen=True)
class AvailabilityRuleDraft:
    timezone: str
    rrule: str
    start_time: time
    end_time: time
    entity_id: uuid.UUID | None = None
    location_entity_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AvailabilityExceptionDraft:
    timezone: str
    exception_date: date
    is_available: bool
    entity_id: uuid.UUID | None = None
    location_entity_id: uuid.UUID | None = None
    availability_rule_id: uuid.UUID | None = None
    start_time: time | None = None
    end_time: time | None = None
    public_message: str | None = None


async def _audit(
    session: AsyncSession,
    actor: Actor,
    workspace_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    fields: list[str] | None = None,
) -> None:
    organization_id = await tenancy.organization_of(session, workspace_id)
    await audit.record(
        session,
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_id=actor.user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        change_diff={"fields": fields} if fields else None,
    )


async def install_pack(session: AsyncSession, actor: Actor, workspace_id: uuid.UUID) -> list[str]:
    """Copy the workspace's pack entity types into it. Idempotent; returns the keys added."""
    require(actor, "packs:install")
    pack = await tenancy.installed_pack(session, workspace_id)
    existing = set(
        (await session.execute(select(EntityType.key, EntityType.schema_version))).tuples()
    )
    added = []
    for spec in pack.entity_types:
        if (spec.key, spec.schema_version) in existing:
            continue
        session.add(
            EntityType(
                workspace_id=workspace_id,
                key=spec.key,
                name=spec.name,
                description=spec.description or None,
                schema_version=spec.schema_version,
                attributes_schema=spec.attributes_schema,
                searchable_fields=list(spec.searchable_fields),
                display_template=spec.display_template or None,
                source="pack",
                source_pack_key=pack.key,
                source_pack_version=pack.version,
                created_by=actor.user_id,
            )
        )
        added.append(spec.key)
    with translate_db_errors():
        await session.flush()
    if added:
        await audit.record(
            session,
            organization_id=await tenancy.organization_of(session, workspace_id),
            workspace_id=workspace_id,
            actor_id=actor.user_id,
            action="pack.install",
            resource_type="workspace",
            resource_id=workspace_id,
            change_diff={"entity_types": added, "pack": f"{pack.key}@{pack.version}"},
        )
    return added


async def _current_type(session: AsyncSession, type_key: str) -> EntityType:
    entity_type = await session.scalar(
        select(EntityType)
        .where(EntityType.key == type_key, EntityType.status == "active")
        .order_by(EntityType.schema_version.desc())
        .limit(1)
    )
    if entity_type is None:
        raise ValidationFailed(errors=[FieldError("type", "Unknown entity type.")])
    return entity_type


def _check_attributes(entity_type: EntityType, attributes: dict[str, Any]) -> None:
    if errors := attribute_errors(entity_type.attributes_schema, attributes):
        raise ValidationFailed(errors=errors)


async def create_entity(
    session: AsyncSession, actor: Actor, *, workspace_id: uuid.UUID, draft: EntityDraft
) -> uuid.UUID:
    require(actor, "catalog:write")
    entity_type = await _current_type(session, draft.type_key)
    _check_attributes(entity_type, draft.attributes)
    entity = Entity(
        workspace_id=workspace_id,
        entity_type_id=entity_type.id,
        key=draft.key,
        name=draft.name,
        aliases=list(draft.aliases),
        attributes=draft.attributes,
        created_by=actor.user_id,
    )
    with translate_db_errors(duplicate="An item with this key already exists."):
        session.add(entity)
        await session.flush()
    await _audit(session, actor, workspace_id, "entity.create", "entity", entity.id)
    return entity.id


async def _live_entity(session: AsyncSession, entity_id: uuid.UUID, row_version: int) -> Entity:
    entity = await session.get(Entity, entity_id)
    if entity is None or entity.deleted_at is not None:
        raise NotFound("Item not found.")
    if entity.row_version != row_version:
        raise Conflict("This item was changed by someone else. Reload and try again.")
    return entity


async def update_entity(
    session: AsyncSession,
    actor: Actor,
    *,
    entity_id: uuid.UUID,
    row_version: int,
    changes: EntityChanges,
    status: str | None = None,
) -> None:
    """Change content and/or publication status under one row_version check (one PATCH)."""
    content = any(v is not None for v in (changes.name, changes.aliases, changes.attributes))
    if content:
        require(actor, "catalog:write")
    if status is not None:
        require(actor, "catalog:publish")
        if status not in PUBLICATION_STATES:
            raise ValidationFailed(errors=[FieldError("publication_status", "Unknown status.")])
    if not content and status is None:
        raise ValidationFailed("Nothing to change.")
    entity = await _live_entity(session, entity_id, row_version)
    changed: list[str] = []
    if changes.attributes is not None:
        entity_type = await session.get(EntityType, entity.entity_type_id)
        assert entity_type is not None  # composite FK guarantees it
        _check_attributes(entity_type, changes.attributes)
        entity.attributes, changed = changes.attributes, [*changed, "attributes"]
    if changes.name is not None:
        entity.name, changed = changes.name, [*changed, "name"]
    if changes.aliases is not None:
        entity.aliases, changed = list(changes.aliases), [*changed, "aliases"]
    if status is not None:
        entity.publication_status, changed = status, [*changed, "publication_status"]
    entity.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, entity.workspace_id, "entity.update", "entity", entity.id, changed)


async def set_entity_status(
    session: AsyncSession, actor: Actor, *, entity_id: uuid.UUID, row_version: int, status: str
) -> None:
    """Publish (visible in the next release), archive, or return to draft."""
    await update_entity(
        session,
        actor,
        entity_id=entity_id,
        row_version=row_version,
        changes=EntityChanges(),
        status=status,
    )


async def delete_entity(
    session: AsyncSession, actor: Actor, *, entity_id: uuid.UUID, row_version: int
) -> None:
    """Soft delete: hidden everywhere, key reusable, history kept for audit."""
    require(actor, "catalog:write")
    entity = await _live_entity(session, entity_id, row_version)
    entity.deleted_at, entity.publication_status = utc_now(), "archived"
    entity.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, entity.workspace_id, "entity.delete", "entity", entity.id)


async def create_relation(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    relation_type: str,
    from_entity_id: uuid.UUID,
    to_entity_id: uuid.UUID,
    attributes: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Link two entities with a relation the workspace's pack declares (e.g. fees)."""
    require(actor, "catalog:write")
    spec = (await tenancy.installed_pack(session, workspace_id)).relation_type(relation_type)
    if spec is None:
        raise ValidationFailed(errors=[FieldError("relation_type", "Unknown relation type.")])
    rows = await session.execute(
        select(Entity.id, EntityType.key)
        .join(EntityType, EntityType.id == Entity.entity_type_id)
        .where(Entity.id.in_([from_entity_id, to_entity_id]), Entity.deleted_at.is_(None))
    )
    # Iterate explicitly: dict(result) would treat the Result as a mapping (it has keys()).
    types = {entity_id: key for entity_id, key in rows.tuples()}
    if types.get(from_entity_id) != spec.from_type or types.get(to_entity_id) != spec.to_type:
        raise ValidationFailed(f"{relation_type} links a {spec.from_type} to a {spec.to_type}.")
    if errors := attribute_errors(spec.attributes_schema, attributes or {}):
        raise ValidationFailed(errors=errors)
    relation = EntityRelation(
        workspace_id=workspace_id,
        from_entity_id=from_entity_id,
        to_entity_id=to_entity_id,
        relation_type=relation_type,
        attributes=attributes or None,
        created_by=actor.user_id,
    )
    with translate_db_errors(duplicate="These items are already linked for this period."):
        session.add(relation)
        await session.flush()
    await _audit(session, actor, workspace_id, "relation.create", "relation", relation.id)
    return relation.id


async def delete_relation(session: AsyncSession, actor: Actor, *, relation_id: uuid.UUID) -> None:
    require(actor, "catalog:write")
    relation = await session.get(EntityRelation, relation_id)
    if relation is None or relation.deleted_at is not None:
        raise NotFound("Link not found.")
    relation.deleted_at, relation.updated_by = utc_now(), actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, relation.workspace_id, "relation.delete", "relation", relation.id)


def _check_timezone(timezone: str) -> None:
    if timezone not in _TIMEZONES:
        raise ValidationFailed(errors=[FieldError("timezone", "Unknown time zone.")])


async def _check_availability_subject(
    session: AsyncSession, workspace_id: uuid.UUID, entity_ids: list[uuid.UUID]
) -> None:
    allowed = set((await tenancy.installed_pack(session, workspace_id)).availability_for)
    found = await session.scalars(
        select(EntityType.key)
        .join(Entity, Entity.entity_type_id == EntityType.id)
        .where(Entity.id.in_(entity_ids), Entity.deleted_at.is_(None))
    )
    kinds = list(found)
    if len(kinds) != len(set(entity_ids)) or set(kinds) - allowed:
        raise ValidationFailed("Hours can only be set for: " + ", ".join(sorted(allowed)) + ".")


async def add_availability_rule(
    session: AsyncSession, actor: Actor, *, workspace_id: uuid.UUID, draft: AvailabilityRuleDraft
) -> uuid.UUID:
    require(actor, "catalog:write")
    _check_timezone(draft.timezone)
    if draft.end_time <= draft.start_time:
        raise ValidationFailed(errors=[FieldError("end_time", "Must be after the start time.")])
    try:
        rrulestr(draft.rrule, dtstart=utc_now())
    except (ValueError, TypeError):
        raise ValidationFailed(errors=[FieldError("rrule", "Invalid recurrence rule.")]) from None
    subjects = [i for i in (draft.entity_id, draft.location_entity_id) if i is not None]
    if not subjects:
        raise ValidationFailed("Choose who or where these hours are for.")
    await _check_availability_subject(session, workspace_id, subjects)
    rule = AvailabilityRule(
        workspace_id=workspace_id,
        entity_id=draft.entity_id,
        location_entity_id=draft.location_entity_id,
        timezone=draft.timezone,
        rrule=draft.rrule,
        start_time=draft.start_time,
        end_time=draft.end_time,
        created_by=actor.user_id,
    )
    with translate_db_errors():
        session.add(rule)
        await session.flush()
    await _audit(session, actor, workspace_id, "availability.create", "availability_rule", rule.id)
    return rule.id


async def add_availability_exception(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    draft: AvailabilityExceptionDraft,
) -> uuid.UUID:
    require(actor, "catalog:write")
    _check_timezone(draft.timezone)
    if (draft.start_time is None) != (draft.end_time is None):
        raise ValidationFailed("Give both a start and an end time, or neither.")
    if draft.start_time and draft.end_time and draft.end_time <= draft.start_time:
        raise ValidationFailed(errors=[FieldError("end_time", "Must be after the start time.")])
    subjects = [i for i in (draft.entity_id, draft.location_entity_id) if i is not None]
    if not subjects and draft.availability_rule_id is None:
        raise ValidationFailed("Choose who or where this exception is for.")
    if subjects:
        await _check_availability_subject(session, workspace_id, subjects)
    exception = AvailabilityException(
        workspace_id=workspace_id,
        availability_rule_id=draft.availability_rule_id,
        entity_id=draft.entity_id,
        location_entity_id=draft.location_entity_id,
        timezone=draft.timezone,
        exception_date=draft.exception_date,
        is_available=draft.is_available,
        start_time=draft.start_time,
        end_time=draft.end_time,
        public_message=draft.public_message,
        created_by=actor.user_id,
    )
    with translate_db_errors(reference="That rule doesn't exist."):
        session.add(exception)
        await session.flush()
    await _audit(
        session,
        actor,
        workspace_id,
        "availability.exception",
        "availability_exception",
        exception.id,
    )
    return exception.id


Detail = EntityRelation | AvailabilityRule | AvailabilityException
_DETAILS: dict[str, type[Detail]] = {
    "relation": EntityRelation,
    "availability_rule": AvailabilityRule,
    "availability_exception": AvailabilityException,
}


async def set_detail_status(
    session: AsyncSession, actor: Actor, *, kind: str, detail_id: uuid.UUID, status: str
) -> None:
    """Publish or archive a relation or an availability rule/exception."""
    require(actor, "catalog:publish")
    model = _DETAILS.get(kind)
    if model is None or status not in PUBLICATION_STATES:
        raise ValidationFailed("Unknown item or status.")
    detail: Detail | None = await session.get(model, detail_id)
    if detail is None or detail.deleted_at is not None:
        raise NotFound("Item not found.")
    detail.publication_status, detail.updated_by = status, actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, detail.workspace_id, f"{kind}.{status}", kind, detail.id)
