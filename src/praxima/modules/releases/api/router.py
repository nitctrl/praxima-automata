"""Agent release endpoints: preview, publish, roll back, history. No logic or queries."""

import uuid

from fastapi import APIRouter, Response, status

from praxima.entrypoints.http.deps import Paging, WorkspaceAccess
from praxima.entrypoints.http.responses import Page
from praxima.modules import iam, releases
from praxima.modules.releases.api.schemas import (
    PreviewOut,
    PublishIn,
    ReleaseDetailOut,
    ReleaseOut,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/agents/{agent_id}", tags=["releases"])


def _workspace(access: WorkspaceAccess) -> uuid.UUID:
    assert access.workspace_id is not None  # WorkspaceAccess always scopes a workspace
    return access.workspace_id


@router.post("/releases/preview")
async def preview(agent_id: uuid.UUID, access: WorkspaceAccess) -> PreviewOut:
    """Build what would be published now (nothing is stored) and compare it with live."""
    return PreviewOut.of(
        await releases.preview_release(
            access.session, access.actor, workspace_id=_workspace(access), agent_id=agent_id
        )
    )


@router.get("/releases")
async def list_releases(
    agent_id: uuid.UUID, access: WorkspaceAccess, page: Paging
) -> Page[ReleaseOut]:
    iam.require(access.actor, "releases:read")
    result = await releases.releases_page(access.session, page, agent_id=agent_id)
    return Page.build(result, [ReleaseOut.of(r) for r in result.items], page.limit)


@router.post("/releases", status_code=status.HTTP_201_CREATED)
async def publish(
    agent_id: uuid.UUID, body: PublishIn, access: WorkspaceAccess, response: Response
) -> ReleaseOut:
    """Publish the previewed digest (409 if content changed since), or roll back."""
    if body.digest is not None:
        release_id = await releases.publish_release(
            access.session,
            access.actor,
            workspace_id=_workspace(access),
            agent_id=agent_id,
            digest=body.digest,
        )
    else:
        assert body.source_release_id is not None
        release_id = await releases.rollback_release(
            access.session,
            access.actor,
            workspace_id=_workspace(access),
            agent_id=agent_id,
            source_release_id=body.source_release_id,
        )
    view, _ = await releases.get_release(access.session, release_id, agent_id=agent_id)
    response.headers["Location"] = (
        f"/api/v1/workspaces/{access.workspace_id}/agents/{agent_id}/releases/{release_id}"
    )
    return ReleaseOut.of(view)


@router.get("/releases/{release_id}")
async def read_release(
    agent_id: uuid.UUID, release_id: uuid.UUID, access: WorkspaceAccess
) -> ReleaseDetailOut:
    """A release with its full snapshot: exactly what the agent uses."""
    iam.require(access.actor, "releases:read")
    view, snapshot = await releases.get_release(access.session, release_id, agent_id=agent_id)
    return ReleaseDetailOut(release=ReleaseOut.of(view), snapshot=snapshot)
