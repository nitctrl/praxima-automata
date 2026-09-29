"""Writes for organizations and workspaces: permission checks, validation, audit."""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, fields
from typing import Any
from zoneinfo import available_timezones

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import audit
from praxima.modules.iam import Actor, active_memberships, require, set_membership
from praxima.modules.tenancy.application.selectors import (
    installed_pack,
    shipped_pack,
    upgrade_problems,
    version_key,
)
from praxima.modules.tenancy.infrastructure.models import Organization, PackVersion, Workspace
from praxima.packs.loader import Pack
from praxima.shared.db.engine import Scope, apply_scope
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import (
    Conflict,
    FieldError,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)

logger = logging.getLogger(__name__)
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


# Platform admin writes go through owner-owned functions (migration 0010): the API's login
# may only read the platform tables. Those functions check the admin again in the database.
_REGISTER = text(
    "SELECT tenancy.admin_register_pack_version(:key, :version, CAST(:manifest AS jsonb), :sum)"
)
_SET_STATUS = text("SELECT tenancy.admin_set_pack_status(:key, :version, :status)")


async def register_shipped_pack(session: AsyncSession, actor: Actor, key: str) -> tuple[str, bool]:
    """Register the shipped version of a pack: (version, newly registered?)."""
    if not actor.is_platform_admin:
        raise PermissionDenied()
    pack = await asyncio.to_thread(shipped_pack, key)
    with translate_db_errors():
        outcome = await session.scalar(
            _REGISTER,
            {
                "key": pack.key,
                "version": pack.version,
                "manifest": json.dumps(pack.payload()),
                "sum": pack.checksum(),
            },
        )
    if outcome == "conflict":
        raise Conflict(
            f"{pack.key} {pack.version} is already registered with different files. "
            "Bump `version` in its manifest.yaml, then register again."
        )
    if outcome == "registered":
        logger.info("Pack %s %s registered by %s", pack.key, pack.version, actor.user_id)
    return pack.version, outcome == "registered"


async def set_pack_status(
    session: AsyncSession, actor: Actor, *, key: str, version: str, status: str
) -> None:
    """available: new workspaces may use it; deprecated/withdrawn: hidden from new ones.

    Workspaces already on that version keep working either way.
    """
    if not actor.is_platform_admin:
        raise PermissionDenied()
    with translate_db_errors():
        found = await session.scalar(
            _SET_STATUS, {"key": key, "version": version, "status": status}
        )
    if not found:
        raise NotFound("No such pack version.")
    logger.info("Pack %s %s set to %s by %s", key, version, status, actor.user_id)


async def upgrade_workspace_pack(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    version: str,
    row_version: int,
) -> None:
    """Move the workspace to a newer available version of its pack.

    Only compatible versions (see `upgrade_problems`). The caller then installs the new
    version's entity types and work item kinds; existing data keeps its schema version.
    """
    require(actor, "packs:install")
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.deleted_at is not None:
        raise NotFound("Workspace not found.")
    if workspace.row_version != row_version:
        raise Conflict("This workspace was changed by someone else. Reload and try again.")
    target = await session.get(PackVersion, (workspace.pack_key, version))
    if target is None or target.status != "available":
        raise ValidationFailed(errors=[FieldError("version", "Pack version unavailable.")])
    if version_key(version) <= version_key(workspace.pack_version):
        raise ValidationFailed(
            errors=[FieldError("version", "Choose a newer version than the installed one.")]
        )
    current = await installed_pack(session, workspace_id)
    if problems := upgrade_problems(current, Pack.from_payload(target.manifest)):
        raise ValidationFailed(
            f"Version {version} isn't compatible with this workspace's data. " + " ".join(problems)
        )
    previous = workspace.pack_version
    workspace.pack_version = version
    workspace.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await audit.record(
        session,
        organization_id=workspace.organization_id,
        workspace_id=workspace.id,
        actor_id=actor.user_id,
        action="pack.upgrade",
        resource_type="workspace",
        resource_id=workspace.id,
        change_diff={"pack": workspace.pack_key, "from": previous, "to": version},
    )


async def set_organization_status(
    session: AsyncSession, actor: Actor, *, organization_id: uuid.UUID, status: str
) -> None:
    """Platform admins suspend or reactivate an organization.

    Suspended: its members get 403 on every workspace and organization endpoint, and calls
    to its numbers hear that information is unavailable (releases.live_release_for_number).
    Nothing is deleted; reactivating restores everything.
    """
    if not actor.is_platform_admin:
        raise PermissionDenied()
    # organizations_update RLS: only the scoped organization.
    await apply_scope(session, Scope(user_id=actor.user_id, organization_id=organization_id))
    organization = await session.get(Organization, organization_id)
    if organization is None or organization.deleted_at is not None:
        raise NotFound("Organization not found.")
    if organization.status == status:
        return
    previous = organization.status
    organization.status = status
    organization.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor.user_id,
        action=f"organization.{'suspend' if status == 'suspended' else 'reactivate'}",
        resource_type="organization",
        resource_id=organization_id,
        change_diff={"status": {"from": previous, "to": status}},
    )
