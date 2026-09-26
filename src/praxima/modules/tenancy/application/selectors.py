"""Reads for organizations and workspaces. RLS limits results to the caller's scope."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules.tenancy.infrastructure.models import Organization, PackVersion, Workspace
from praxima.packs.loader import Pack
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


async def installed_pack(session: AsyncSession, workspace_id: uuid.UUID) -> Pack:
    """The pack version this workspace runs, from the stored registry payload (one query)."""
    payload = await session.scalar(
        select(PackVersion.manifest)
        .join(
            Workspace,
            (Workspace.pack_key == PackVersion.pack_key)
            & (Workspace.pack_version == PackVersion.version),
        )
        .where(Workspace.id == workspace_id, Workspace.deleted_at.is_(None))
    )
    if payload is None:
        raise NotFound("Workspace not found.")
    return Pack.from_payload(payload)


@dataclass(frozen=True)
class PackVersionView:
    key: str
    version: str
    name: str
    industry: str


async def available_packs(session: AsyncSession) -> list[PackVersionView]:
    """Pack versions that new workspaces may use (platform table, no tenant)."""
    rows = await session.scalars(
        select(PackVersion)
        .where(PackVersion.status == "available")
        .order_by(PackVersion.pack_key, PackVersion.version)
    )
    return [
        PackVersionView(
            p.pack_key,
            p.version,
            p.manifest.get("name", p.pack_key),
            p.manifest.get("industry", ""),
        )
        for p in rows
    ]
