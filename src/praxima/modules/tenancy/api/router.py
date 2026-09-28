"""Workspace endpoints. No business logic or queries here."""

from fastapi import APIRouter, Response, status

from praxima.entrypoints.http.deps import (
    CurrentUser,
    OrganizationAccess,
    SelfSignup,
    UserSession,
    WorkspaceAccess,
    narrow_to_workspace,
)
from praxima.entrypoints.http.responses import Page, PageInfo
from praxima.modules import catalog, engagement, iam, tenancy
from praxima.modules.tenancy.api.schemas import (
    OrganizationIn,
    OrganizationOut,
    PackOut,
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
