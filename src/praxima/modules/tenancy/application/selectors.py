"""Reads for organizations and workspaces. RLS limits results to the caller's scope."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select
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
    organization_status: str  # "suspended": its members can't use it until reactivated


def _view(w: Workspace, organization_status: str | None) -> WorkspaceView:
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
        organization_status or "active",
    )


def _with_organization_status() -> Select[tuple[Workspace, str]]:
    return select(Workspace, Organization.status).outerjoin(
        Organization, Organization.id == Workspace.organization_id
    )


async def visible_workspaces(session: AsyncSession) -> list[WorkspaceView]:
    """Every workspace the scope may see; with a user-only scope, exactly their memberships."""
    rows = await session.execute(
        _with_organization_status()
        .where(Workspace.deleted_at.is_(None))
        .order_by(Workspace.name, Workspace.id)
    )
    return [_view(w, status) for w, status in rows.tuples()]


async def get_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> WorkspaceView:
    row = (
        await session.execute(
            _with_organization_status().where(
                Workspace.id == workspace_id, Workspace.deleted_at.is_(None)
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Workspace not found.")
    return _view(row[0], row[1])


async def organization_of(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    organization_id = await session.scalar(
        select(Workspace.organization_id).where(
            Workspace.id == workspace_id, Workspace.deleted_at.is_(None)
        )
    )
    if organization_id is None:
        raise NotFound("Workspace not found.")
    return organization_id


async def organization_and_status_of(
    session: AsyncSession, workspace_id: uuid.UUID
) -> tuple[uuid.UUID, str]:
    """The workspace's organization and that organization's status, in one query."""
    row = (
        await session.execute(
            select(Workspace.organization_id, Organization.status)
            .outerjoin(Organization, Organization.id == Workspace.organization_id)
            .where(Workspace.id == workspace_id, Workspace.deleted_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Workspace not found.")
    return row[0], row[1] or "active"


async def organization_status(session: AsyncSession, organization_id: uuid.UUID) -> str | None:
    """active, suspended or closed; None when the scope can't see the organization."""
    status: str | None = await session.scalar(
        select(Organization.status).where(
            Organization.id == organization_id, Organization.deleted_at.is_(None)
        )
    )
    return status


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
        status = {o.id: o.status for o in organizations}
        for w in rows:
            workspaces[w.organization_id].append(_view(w, status[w.organization_id]))
    return PageResult(
        [
            OrganizationSummary(o.id, o.slug, o.name, o.status, o.created_at, workspaces[o.id])
            for o in organizations
        ],
        result.next_cursor,
    )


async def organization_summary(
    session: AsyncSession, organization_id: uuid.UUID
) -> OrganizationSummary:
    organization = await session.get(Organization, organization_id)
    if organization is None or organization.deleted_at is not None:
        raise NotFound("Organization not found.")
    rows = await session.scalars(
        select(Workspace)
        .where(Workspace.organization_id == organization_id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.name, Workspace.id)
    )
    o = organization
    return OrganizationSummary(
        o.id, o.slug, o.name, o.status, o.created_at, [_view(w, o.status) for w in rows]
    )


# ------------------------------------------------------------------ pack upgrades


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def upgrade_problems(current: Pack, target: Pack) -> list[str]:
    """Why `target` can't replace `current` in a workspace that has data (empty = safe).

    A version may add types, kinds and links, and change wording. It may not remove anything
    existing data uses, or change a schema without a new schema_version (existing rows keep
    the version they were written with; new rows use the newest).
    """
    problems = []
    new_types = {t.key: t for t in target.entity_types}
    for old in current.entity_types:
        new = new_types.get(old.key)
        if new is None:
            problems.append(f"Removes the entry type '{old.key}'.")
        elif new.schema_version < old.schema_version or (
            new.schema_version == old.schema_version
            and new.attributes_schema != old.attributes_schema
        ):
            problems.append(f"Changes the '{old.key}' fields without a new schema_version.")
    new_kinds = {k.key: k for k in target.work_item_kinds}
    for kind in current.work_item_kinds:
        after = new_kinds.get(kind.key)
        rules = ("payload_schema", "stages", "initial_stage", "terminal_stages", "subject_types")
        if after is None:
            problems.append(f"Removes the request type '{kind.key}'.")
        elif after.schema_version < kind.schema_version or (
            after.schema_version == kind.schema_version
            and any(getattr(after, r) != getattr(kind, r) for r in rules)
        ):
            problems.append(f"Changes the '{kind.key}' request without a new schema_version.")
    new_links = {r.key: r for r in target.relation_types}
    for link in current.relation_types:
        joined = new_links.get(link.key)
        if joined is None:
            problems.append(f"Removes the link type '{link.key}'.")
        elif (joined.from_type, joined.to_type) != (link.from_type, link.to_type):
            problems.append(f"Changes what the '{link.key}' link connects.")
    return problems


@dataclass(frozen=True)
class PackUpgradeView:
    version: str
    problems: list[str]  # empty: the workspace can upgrade to this version


async def pack_upgrades(session: AsyncSession, workspace_id: uuid.UUID) -> list[PackUpgradeView]:
    """Newer available versions of the workspace's pack, oldest first, each checked."""
    current = await installed_pack(session, workspace_id)
    rows = await session.scalars(
        select(PackVersion).where(
            PackVersion.pack_key == current.key, PackVersion.status == "available"
        )
    )
    newer = sorted(
        (p for p in rows if version_key(p.version) > version_key(current.version)),
        key=lambda p: version_key(p.version),
    )
    return [
        PackUpgradeView(p.version, upgrade_problems(current, Pack.from_payload(p.manifest)))
        for p in newer
    ]
