"""Reads for organizations and workspaces. RLS limits results to the caller's scope."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules.tenancy.infrastructure.models import Organization, PackVersion, Workspace
from praxima.packs import loader
from praxima.packs.loader import Pack
from praxima.shared.db.pagination import PageRequest, PageResult, fetch_page
from praxima.shared.errors import NotFound, ValidationFailed


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


@dataclass(frozen=True)
class RegisteredPackView:
    key: str
    version: str
    name: str
    industry: str
    status: str
    checksum: str
    released_at: datetime


async def registered_packs(session: AsyncSession) -> list[RegisteredPackView]:
    """Every registered pack version, whatever its status (for platform admins)."""
    rows = await session.scalars(
        select(PackVersion).order_by(PackVersion.pack_key, PackVersion.version)
    )
    return [
        RegisteredPackView(
            p.pack_key,
            p.version,
            p.manifest.get("name", p.pack_key),
            p.manifest.get("industry", ""),
            p.status,
            p.checksum,
            p.released_at,
        )
        for p in rows
    ]


def shipped_pack(key: str) -> Pack:
    """A pack shipped in src/praxima/packs (blocking file reads: call it in a thread)."""
    if key not in loader.available():  # never a path from the request
        raise NotFound("No such pack.")
    try:
        return loader.load(key)
    except loader.PackError:
        raise ValidationFailed("This pack's files are invalid. Fix them, then retry.") from None


@dataclass(frozen=True)
class PackCatalogEntry:
    """One pack: what ships with this build and which versions are registered."""

    key: str
    name: str
    industry: str
    shipped_version: str | None  # None: registered earlier but no longer shipped
    shipped_invalid: bool
    needs_registration: bool
    files_changed_without_bump: bool
    versions: list[RegisteredPackView]


def _load_shipped() -> dict[str, Pack | None]:
    found: dict[str, Pack | None] = {}
    for key in loader.available():
        try:
            found[key] = loader.load(key)
        except loader.PackError:
            found[key] = None
    return found


async def pack_catalog(session: AsyncSession) -> list[PackCatalogEntry]:
    """Every shipped or registered pack, for platform admins."""
    registered = await registered_packs(session)
    shipped = await asyncio.to_thread(_load_shipped)
    entries = []
    for key in sorted(set(shipped) | {r.key for r in registered}):
        versions = [r for r in registered if r.key == key]
        pack = shipped.get(key)
        same = next((r for r in versions if pack and r.version == pack.version), None)
        latest = versions[-1] if versions else None
        entries.append(
            PackCatalogEntry(
                key=key,
                name=pack.name if pack else latest.name if latest else key,
                industry=pack.industry if pack else latest.industry if latest else "",
                shipped_version=pack.version if pack else None,
                shipped_invalid=key in shipped and pack is None,
                needs_registration=pack is not None and same is None,
                files_changed_without_bump=bool(pack and same and same.checksum != pack.checksum()),
                versions=versions,
            )
        )
    return entries


@dataclass(frozen=True)
class OrganizationSummary:
    id: uuid.UUID
    slug: str
    name: str
    status: str
    created_at: datetime
    workspaces: list[WorkspaceView]


async def organizations_page(session: AsyncSession, page: PageRequest) -> PageResult:
    """Organizations newest first, each with its workspaces (two queries per page).

    RLS shows a platform admin every organization; anyone else only their own.
    """
    result = await fetch_page(
        session, select(Organization), (Organization.created_at, Organization.id), page
    )
    organizations: list[Organization] = result.items
    workspaces: dict[uuid.UUID, list[WorkspaceView]] = {o.id: [] for o in organizations}
    if organizations:
        rows = await session.scalars(
            select(Workspace)
            .where(Workspace.organization_id.in_(list(workspaces)), Workspace.deleted_at.is_(None))
            .order_by(Workspace.name, Workspace.id)
        )
        for w in rows:
            workspaces[w.organization_id].append(_view(w))
    return PageResult(
        [
            OrganizationSummary(o.id, o.slug, o.name, o.status, o.created_at, workspaces[o.id])
            for o in organizations
        ],
        result.next_cursor,
    )
