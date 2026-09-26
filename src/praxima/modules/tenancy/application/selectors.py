"""Reads for organizations and workspaces. RLS limits results to the caller's scope."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules.tenancy.infrastructure.models import Organization, Workspace
from praxima.shared.errors import NotFound


@dataclass(frozen=True)
class WorkspaceView:
    id: uuid.UUID
    organization_id: uuid.UUID
    slug: str
    name: str
    industry: str
    pack_key: str
    pack_version: str
    timezone: str
    default_language: str
    supported_languages: list[str]
    status: str
    row_version: int


def _view(w: Workspace) -> WorkspaceView:
    return WorkspaceView(
        w.id,
        w.organization_id,
        w.slug,
        w.name,
        w.industry,
        w.pack_key,
        w.pack_version,
        w.timezone,
        w.default_language,
        list(w.supported_languages),
        w.status,
        w.row_version,
    )


async def visible_workspaces(session: AsyncSession) -> list[WorkspaceView]:
    """Every workspace the scope may see; with a user-only scope, exactly their memberships."""
    rows = await session.scalars(
        select(Workspace)
        .where(Workspace.deleted_at.is_(None))
        .order_by(Workspace.name, Workspace.id)
    )
    return [_view(w) for w in rows]


async def get_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> WorkspaceView:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.deleted_at is not None:
        raise NotFound("Workspace not found.")
    return _view(workspace)


async def organization_of(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    organization_id = await session.scalar(
        select(Workspace.organization_id).where(
            Workspace.id == workspace_id, Workspace.deleted_at.is_(None)
        )
    )
    if organization_id is None:
        raise NotFound("Workspace not found.")
    return organization_id


async def organization_exists(session: AsyncSession, organization_id: uuid.UUID) -> bool:
    return (
        await session.scalar(
            select(Organization.id).where(
                Organization.id == organization_id, Organization.deleted_at.is_(None)
            )
        )
    ) is not None
