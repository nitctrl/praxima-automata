"""Agent endpoints: configuration, tools and phone numbers. No logic or queries here."""

import uuid

from fastapi import APIRouter, Request, Response, status

from praxima.entrypoints.http.deps import WorkspaceAccess
from praxima.entrypoints.http.responses import Page, PageInfo
from praxima.modules import agents, iam
from praxima.modules.agents.api.schemas import (
    AgentIn,
    AgentOut,
    AgentPatch,
    PhoneNumberIn,
    PhoneNumberOut,
    ToolIn,
    ToolOut,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/agents", tags=["agents"])


@router.get("")
async def list_agents(access: WorkspaceAccess) -> Page[AgentOut]:
    iam.require(access.actor, "agents:read")
    items = [AgentOut.of(a) for a in await agents.list_agents(access.session)]
    return Page(data=items, page=PageInfo(limit=len(items), next_cursor=None))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentIn, access: WorkspaceAccess, request: Request, response: Response
) -> AgentOut:
    assert access.workspace_id is not None
    agent_id = await agents.create_agent(
        access.session, access.actor, workspace_id=access.workspace_id, draft=body.draft()
    )
    response.headers["Location"] = f"{request.url.path}/{agent_id}"
    return AgentOut.of(await agents.get_agent(access.session, agent_id))


@router.get("/{agent_id}")
async def read_agent(agent_id: uuid.UUID, access: WorkspaceAccess) -> AgentOut:
    iam.require(access.actor, "agents:read")
    return AgentOut.of(await agents.get_agent(access.session, agent_id))


@router.patch("/{agent_id}")
async def update_agent(agent_id: uuid.UUID, body: AgentPatch, access: WorkspaceAccess) -> AgentOut:
    await agents.update_agent(
        access.session,
        access.actor,
        agent_id=agent_id,
        row_version=body.row_version,
        changes=body.changes(),
    )
    return AgentOut.of(await agents.get_agent(access.session, agent_id))


@router.put("/{agent_id}/tools/{tool_key}")
async def set_tool(
    agent_id: uuid.UUID, tool_key: str, body: ToolIn, access: WorkspaceAccess
) -> ToolOut:
    await agents.set_tool(
        access.session,
        access.actor,
        agent_id=agent_id,
        tool_key=tool_key,
        enabled=body.enabled,
        config=body.config,
    )
    return ToolOut(key=tool_key, enabled=body.enabled)


@router.post("/{agent_id}/phone-numbers", status_code=status.HTTP_201_CREATED)
async def assign_phone_number(
    agent_id: uuid.UUID, body: PhoneNumberIn, access: WorkspaceAccess
) -> PhoneNumberOut:
    number_id = await agents.assign_phone_number(
        access.session,
        access.actor,
        agent_id=agent_id,
        phone_number=body.phone_number,
        provider=body.provider,
        trusted_trunk_id=body.trusted_trunk_id,
    )
    view = await agents.get_agent(access.session, agent_id)
    number = next(n for n in view.phone_numbers if n.id == number_id)
    return PhoneNumberOut(**number.__dict__)


@router.delete("/{agent_id}/phone-numbers/{phone_number_id}", status_code=204)
async def release_phone_number(
    agent_id: uuid.UUID, phone_number_id: uuid.UUID, access: WorkspaceAccess
) -> None:
    await agents.release_phone_number(
        access.session, access.actor, phone_number_id=phone_number_id, agent_id=agent_id
    )
