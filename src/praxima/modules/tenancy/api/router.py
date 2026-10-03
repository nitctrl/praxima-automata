"""Workspace endpoints. No business logic or queries here."""

import uuid

from fastapi import APIRouter, Response, status

from praxima.entrypoints.http.deps import (
    CurrentUser,
    OrganizationAccess,
    Paging,
    PlatformAccess,
    SelfSignup,
    UserSession,
    WorkspaceAccess,
    narrow_to_workspace,
)
from praxima.entrypoints.http.responses import Page, PageInfo
from praxima.modules import catalog, engagement, iam, tenancy
from praxima.modules.tenancy.api.schemas import (
    AgentDefaultsOut,
    EntityLabelOut,
    OrganizationIn,
    OrganizationOut,
    OrganizationStatusIn,
    OrganizationSummaryOut,
    PackBookingOut,
    PackCatalogOut,
    PackDetailsOut,
    PackKey,
    PackOut,
    PackRegistrationOut,
    PackStatusIn,
    PackUpgradeIn,
    PackUpgradeOut,
    PackVersionText,
    WorkspaceIn,
    WorkspaceOut,
    WorkspacePatch,
)
from praxima.shared.errors import PermissionDenied

router = APIRouter(tags=["workspaces"])


@router.get("/workspaces")
async def list_workspaces(session: UserSession) -> Page[WorkspaceOut]:
    """Every workspace the signed-in user belongs to (a short, complete list)."""
    workspaces = [WorkspaceOut.of(w) for w in await tenancy.visible_workspaces(session)]
    return Page(data=workspaces, page=PageInfo(limit=len(workspaces), next_cursor=None))


@router.get("/workspaces/{workspace_id}")
async def read_workspace(access: WorkspaceAccess) -> WorkspaceOut:
    iam.require(access.actor, "workspace:read")
    assert access.workspace_id is not None
    return WorkspaceOut.of(await tenancy.get_workspace(access.session, access.workspace_id))


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(body: WorkspacePatch, access: WorkspaceAccess) -> WorkspaceOut:
    assert access.workspace_id is not None
    await tenancy.update_workspace(
        access.session,
        access.actor,
        workspace_id=access.workspace_id,
        row_version=body.row_version,
        changes=body.changes(),
    )
    return WorkspaceOut.of(await tenancy.get_workspace(access.session, access.workspace_id))


@router.get("/workspaces/{workspace_id}/pack")
async def read_workspace_pack(access: WorkspaceAccess) -> PackDetailsOut:
    """The installed pack's vocabulary: labels, categories, kinds and starter wording."""
    iam.require(access.actor, "workspace:read")
    assert access.workspace_id is not None
    pack = await tenancy.installed_pack(access.session, access.workspace_id)
    return PackDetailsOut(
        key=pack.key,
        version=pack.version,
        name=pack.name,
        industry=pack.industry,
        entity_labels={
            t.key: EntityLabelOut(name=t.name, plural_name=t.plural_name) for t in pack.entity_types
        },
        document_categories=list(pack.document_categories),
        announcement_kinds=list(pack.announcement_kinds),
        callback_kind=pack.callback_kind,
        agent_defaults=AgentDefaultsOut(**pack.agent_defaults.model_dump())
        if pack.agent_defaults
        else None,
        booking=PackBookingOut(
            label=pack.booking.label,
            plural_label=pack.booking.plural_label or f"{pack.booking.label}s",
            resource_types=list(pack.booking.resource_types),
            subject_types=list(pack.booking.subject_types),
            slot_minutes=pack.booking.slot_minutes,
        )
        if pack.booking
        else None,
        upgrades=[
            PackUpgradeOut(version=u.version, problems=u.problems)
            for u in await tenancy.pack_upgrades(access.session, access.workspace_id)
        ],
    )


@router.post("/workspaces/{workspace_id}/pack-upgrades")
async def upgrade_workspace_pack(body: PackUpgradeIn, access: WorkspaceAccess) -> WorkspaceOut:
    """Move to a newer compatible version of the installed pack and install what it adds
    (admin+). Existing entries and requests keep their schema; new ones use the newest."""
    assert access.workspace_id is not None
    await tenancy.upgrade_workspace_pack(
        access.session,
        access.actor,
        workspace_id=access.workspace_id,
        version=body.version,
        row_version=body.row_version,
    )
    await catalog.install_pack(access.session, access.actor, access.workspace_id)
    await engagement.install_work_item_kinds(access.session, access.actor, access.workspace_id)
    return WorkspaceOut.of(await tenancy.get_workspace(access.session, access.workspace_id))


@router.get("/packs")
async def list_packs(session: UserSession) -> Page[PackOut]:
    """Domain pack versions a new workspace can use."""
    packs = [PackOut(**p.__dict__) for p in await tenancy.available_packs(session)]
    return Page(data=packs, page=PageInfo(limit=len(packs), next_cursor=None))


@router.post("/organizations/{organization_id}/workspaces", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceIn, access: OrganizationAccess, response: Response
) -> WorkspaceOut:
    """Create a workspace and install its domain pack (entity types, work item kinds)."""
    workspace_id = await tenancy.create_workspace(
        access.session, access.actor, organization_id=access.organization_id, draft=body.draft()
    )
    scoped = await narrow_to_workspace(access, workspace_id)
    await catalog.install_pack(scoped.session, scoped.actor, workspace_id)
    await engagement.install_work_item_kinds(scoped.session, scoped.actor, workspace_id)
    response.headers["Location"] = f"/api/v1/workspaces/{workspace_id}"
    return WorkspaceOut.of(await tenancy.get_workspace(scoped.session, workspace_id))


@router.post("/organizations", status_code=status.HTTP_201_CREATED)
async def create_own_organization(
    body: OrganizationIn,
    user: CurrentUser,
    session: UserSession,
    allowed: SelfSignup,
    response: Response,
) -> OrganizationOut:
    """Self-service sign-up: create your first organization and become its owner."""
    if not allowed:
        raise PermissionDenied("Self-service sign-up is turned off. Ask an administrator.")
    organization_id = await tenancy.create_own_organization(
        session, user.user_id, slug=body.slug, name=body.name.strip()
    )
    response.headers["Location"] = f"/api/v1/organizations/{organization_id}"
    return OrganizationOut(id=organization_id, slug=body.slug, name=body.name.strip())


# Platform admin area: every tenant, so only platform admins (404 for anyone else).


@router.get("/platform/packs", tags=["platform"])
async def list_pack_catalog(access: PlatformAccess) -> Page[PackCatalogOut]:
    """Shipped packs and their registered versions, with what needs attention."""
    packs = [PackCatalogOut.of(p) for p in await tenancy.pack_catalog(access.session)]
    return Page(data=packs, page=PageInfo(limit=len(packs), next_cursor=None))


@router.post(
    "/platform/packs/{key}/registrations",
    tags=["platform"],
    status_code=status.HTTP_201_CREATED,
)
async def register_pack(
    key: PackKey, access: PlatformAccess, response: Response
) -> PackRegistrationOut:
    """Register the shipped version (201), or 200 if already registered with the same files."""
    version, registered = await tenancy.register_shipped_pack(access.session, access.actor, key)
    if not registered:
        response.status_code = status.HTTP_200_OK
    return PackRegistrationOut(key=key, version=version, registered=registered)


@router.patch("/platform/packs/{key}/versions/{version}", tags=["platform"])
async def update_pack_version(
    key: PackKey, version: PackVersionText, body: PackStatusIn, access: PlatformAccess
) -> PackCatalogOut:
    """Make a version available to new workspaces, or deprecate / withdraw it."""
    await tenancy.set_pack_status(
        access.session, access.actor, key=key, version=version, status=body.status
    )
    entry = next(p for p in await tenancy.pack_catalog(access.session) if p.key == key)
    return PackCatalogOut.of(entry)


@router.get("/platform/organizations", tags=["platform"])
async def list_all_organizations(
    access: PlatformAccess, page: Paging
) -> Page[OrganizationSummaryOut]:
    """Every organization, newest first, with its workspaces and member count."""
    result = await tenancy.organizations_page(access.session, page)
    counts = await iam.member_counts(access.session, [o.id for o in result.items])
    return Page.build(
        result,
        [OrganizationSummaryOut.of(o, counts.get(o.id, 0)) for o in result.items],
        page.limit,
    )


@router.patch("/platform/organizations/{organization_id}", tags=["platform"])
async def update_organization_status(
    organization_id: uuid.UUID, body: OrganizationStatusIn, access: PlatformAccess
) -> OrganizationSummaryOut:
    """Suspend (members get 403, calls hear "unavailable") or reactivate an organization."""
    await tenancy.set_organization_status(
        access.session, access.actor, organization_id=organization_id, status=body.status
    )
    summary = await tenancy.organization_summary(access.session, organization_id)
    counts = await iam.member_counts(access.session, [organization_id])
    return OrganizationSummaryOut.of(summary, counts.get(organization_id, 0))
