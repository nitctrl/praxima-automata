"""Writes for organizations and workspaces: permission checks, validation, audit."""

import uuid
from dataclasses import dataclass, fields
from typing import Any
from zoneinfo import available_timezones

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import audit
from praxima.modules.iam import Actor, active_memberships, require, set_membership
from praxima.modules.tenancy.infrastructure.models import Organization, PackVersion, Workspace
from praxima.shared.db.engine import Scope, apply_scope
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import (
    Conflict,
    FieldError,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)

_TIMEZONES = frozenset(available_timezones())


@dataclass(frozen=True)
class WorkspaceDraft:
    slug: str
    name: str
    pack_key: str
    pack_version: str
    timezone: str
    default_language: str
    supported_languages: tuple[str, ...]
    industry: str = ""  # defaults to the pack's industry


@dataclass(frozen=True)
class WorkspaceChanges:
    """PATCH fields; None means "leave unchanged"."""

    name: str | None = None
    timezone: str | None = None
    default_language: str | None = None
    supported_languages: tuple[str, ...] | None = None


def _check_languages(default: str, supported: tuple[str, ...]) -> None:
    if not supported or default not in supported:
        raise ValidationFailed(
            errors=[FieldError("default_language", "Must be one of the supported languages.")]
        )


def _check_timezone(timezone: str) -> None:
    if timezone not in _TIMEZONES:
        raise ValidationFailed(errors=[FieldError("timezone", "Unknown time zone.")])


async def create_organization(
    session: AsyncSession, actor: Actor, *, slug: str, name: str, owner_user_id: uuid.UUID
) -> uuid.UUID:
    """Platform admins onboard a customer organization and name its first owner."""
    if not actor.is_platform_admin:
        raise PermissionDenied()
    organization = Organization(slug=slug, name=name, created_by=actor.user_id)
    with translate_db_errors(duplicate="An organization with this slug already exists."):
        session.add(organization)
        await session.flush()
    await set_membership(
        session,
        actor,
        organization_id=organization.id,
        user_id=owner_user_id,
        role="owner",
    )
    await audit.record(
        session,
        organization_id=organization.id,
        actor_id=actor.user_id,
        action="organization.create",
        resource_type="organization",
        resource_id=organization.id,
    )
    return organization.id


async def create_own_organization(
    session: AsyncSession, user_id: uuid.UUID, *, slug: str, name: str
) -> uuid.UUID:
    """Self-serve sign-up: a person with no organization creates one and becomes its owner.

    The caller (the API) decides whether self-serve sign-up is enabled; the database policy
    `organizations_self_serve` enforces "own it, and only if you have no organization yet".
    """
    if await active_memberships(session, user_id):
        raise Conflict("You already belong to an organization.")
    organization = Organization(slug=slug, name=name, created_by=user_id)
    with translate_db_errors(duplicate="An organization with this short name already exists."):
        session.add(organization)
        await session.flush()
    await apply_scope(session, Scope(user_id=user_id, organization_id=organization.id))
    owner = Actor(user_id, "owner")
    await set_membership(
        session, owner, organization_id=organization.id, user_id=user_id, role="owner"
    )
    await audit.record(
        session,
        organization_id=organization.id,
        actor_id=user_id,
        action="organization.create",
        resource_type="organization",
        resource_id=organization.id,
    )
    return organization.id


async def create_workspace(
    session: AsyncSession, actor: Actor, *, organization_id: uuid.UUID, draft: WorkspaceDraft
) -> uuid.UUID:
    require(actor, "workspace:create")
    _check_timezone(draft.timezone)
    _check_languages(draft.default_language, draft.supported_languages)
    pack = (
        await session.execute(
            select(PackVersion.status, PackVersion.manifest).where(
                PackVersion.pack_key == draft.pack_key, PackVersion.version == draft.pack_version
            )
        )
    ).one_or_none()
    if pack is None or pack.status != "available":
        raise ValidationFailed(errors=[FieldError("pack_version", "Pack version unavailable.")])
    workspace = Workspace(
        organization_id=organization_id,
        slug=draft.slug,
        name=draft.name,
        industry=draft.industry or str(pack.manifest.get("industry", "")),
        pack_key=draft.pack_key,
        pack_version=draft.pack_version,
        timezone=draft.timezone,
        default_language=draft.default_language,
        supported_languages=list(draft.supported_languages),
        created_by=actor.user_id,
    )
    with translate_db_errors(duplicate="A workspace with this slug already exists."):
        session.add(workspace)
        await session.flush()
    await audit.record(
        session,
        organization_id=organization_id,
        workspace_id=workspace.id,
        actor_id=actor.user_id,
        action="workspace.create",
        resource_type="workspace",
        resource_id=workspace.id,
    )
    return workspace.id


async def update_workspace(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    row_version: int,
    changes: WorkspaceChanges,
) -> None:
    """Apply a PATCH if nobody changed the workspace since `row_version` was read."""
    require(actor, "workspace:update")
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.deleted_at is not None:
        raise NotFound("Workspace not found.")
    if workspace.row_version != row_version:
        raise Conflict("This workspace was changed by someone else. Reload and try again.")
    updates: dict[str, Any] = {
        f.name: getattr(changes, f.name)
        for f in fields(changes)
        if getattr(changes, f.name) is not None
    }
    if "timezone" in updates:
        _check_timezone(updates["timezone"])
    default = updates.get("default_language", workspace.default_language)
    supported = updates.get("supported_languages", tuple(workspace.supported_languages))
    _check_languages(default, tuple(supported))
    for name, value in updates.items():
        setattr(workspace, name, list(value) if name == "supported_languages" else value)
    workspace.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await audit.record(
        session,
        organization_id=workspace.organization_id,
        workspace_id=workspace.id,
        actor_id=actor.user_id,
        action="workspace.update",
        resource_type="workspace",
        resource_id=workspace.id,
        change_diff={"fields": sorted(updates)},
    )
