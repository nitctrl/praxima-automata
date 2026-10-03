"""Reads for agents, and the ingress lookup (called number → agent → workspace)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import tenancy
from praxima.modules.agents.infrastructure.models import Agent, AgentTool, PhoneNumber
from praxima.shared.errors import NotFound


@dataclass(frozen=True)
class ToolView:
    key: str
    enabled: bool


@dataclass(frozen=True)
class PhoneNumberView:
    id: uuid.UUID
    phone_number: str
    provider: str
    direction: str
    status: str


@dataclass(frozen=True)
class AgentView:
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    slug: str
    status: str
    persona: str | None
    greeting_message: str
    emergency_message: str
    fallback_message: str
    transfer_enabled: bool
    row_version: int
    tools: list[ToolView]
    phone_numbers: list[PhoneNumberView]


@dataclass(frozen=True)
class IngressTarget:
    phone_number_id: uuid.UUID
    agent_id: uuid.UUID
    workspace_id: uuid.UUID


async def _agents(session: AsyncSession, agent_id: uuid.UUID | None) -> list[AgentView]:
    """Agents with their tools and numbers: four queries however many agents there are.

    Tools the workspace's pack offers but an agent has no setting for yet (added by a pack
    upgrade) are listed as off, so staff can opt in; they never switch on by themselves.
    """
    statement = select(Agent).where(Agent.deleted_at.is_(None)).order_by(Agent.name, Agent.id)
    if agent_id is not None:
        statement = statement.where(Agent.id == agent_id)
    agents = list(await session.scalars(statement))
    ids = [a.id for a in agents]
    tools: dict[uuid.UUID, list[ToolView]] = {i: [] for i in ids}
    numbers: dict[uuid.UUID, list[PhoneNumberView]] = {i: [] for i in ids}
    if ids:
        for tool in await session.scalars(
            select(AgentTool).where(AgentTool.agent_id.in_(ids)).order_by(AgentTool.tool_key)
        ):
            tools[tool.agent_id].append(ToolView(tool.tool_key, tool.enabled))
        for number in await session.scalars(
            select(PhoneNumber)
            .where(PhoneNumber.agent_id.in_(ids), PhoneNumber.status != "released")
            .order_by(PhoneNumber.phone_number)
        ):
            numbers[number.agent_id].append(
                PhoneNumberView(
                    number.id, number.phone_number, number.provider, number.direction, number.status
                )
            )
        offered = (await tenancy.installed_pack(session, agents[0].workspace_id)).tools
        for agent_tools in tools.values():
            have = {t.key for t in agent_tools}
            agent_tools.extend(ToolView(key, False) for key in offered if key not in have)
            agent_tools.sort(key=lambda t: t.key)
    return [
        AgentView(
            a.id,
            a.workspace_id,
            a.name,
            a.slug,
            a.status,
            a.persona,
            a.greeting_message,
            a.emergency_message,
            a.fallback_message,
            a.transfer_enabled,
            a.row_version,
            tools[a.id],
            numbers[a.id],
        )
        for a in agents
    ]


async def list_agents(session: AsyncSession) -> list[AgentView]:
    """Every agent of the scoped workspace."""
    return await _agents(session, None)


async def get_agent(session: AsyncSession, agent_id: uuid.UUID) -> AgentView:
    found = await _agents(session, agent_id)
    if not found:
        raise NotFound("Agent not found.")
    return found[0]


async def resolve_ingress(session: AsyncSession, called_number: str) -> IngressTarget | None:
    """Needs a scope carrying `called_number` from trusted telephony ingress (not the caller)."""
    row = (
        await session.execute(
            select(PhoneNumber.id, PhoneNumber.agent_id, PhoneNumber.workspace_id).where(
                PhoneNumber.phone_number == called_number, PhoneNumber.status == "active"
            )
        )
    ).one_or_none()
    return IngressTarget(row.id, row.agent_id, row.workspace_id) if row else None
