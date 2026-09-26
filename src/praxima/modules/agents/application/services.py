"""Writes for agents: configuration, tools and phone number routing."""

import re
import uuid
from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import audit, tenancy
from praxima.modules.agents.infrastructure.models import Agent, AgentTool, PhoneNumber
from praxima.modules.iam import Actor, require
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import Conflict, FieldError, NotFound, ValidationFailed

E164 = re.compile(r"\+[1-9][0-9]{7,14}")


@dataclass(frozen=True)
class AgentDraft:
    name: str
    slug: str
    greeting_message: str
    emergency_message: str
    fallback_message: str
    persona: str | None = None
    prompt_template_key: str | None = None


@dataclass(frozen=True)
class AgentChanges:
    """PATCH fields; None means "leave unchanged"."""

    name: str | None = None
    status: str | None = None
    persona: str | None = None
    greeting_message: str | None = None
    emergency_message: str | None = None
    fallback_message: str | None = None


async def _audit(
    session: AsyncSession,
    actor: Actor,
    workspace_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    change_diff: dict[str, Any] | None = None,
) -> None:
    await audit.record(
        session,
        organization_id=await tenancy.organization_of(session, workspace_id),
        workspace_id=workspace_id,
        actor_id=actor.user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        change_diff=change_diff,
    )


async def create_agent(
    session: AsyncSession, actor: Actor, *, workspace_id: uuid.UUID, draft: AgentDraft
) -> uuid.UUID:
    """Create an agent with the pack's default tools enabled."""
    require(actor, "agents:write")
    pack = await tenancy.installed_pack(session, workspace_id)
    agent = Agent(workspace_id=workspace_id, created_by=actor.user_id, **draft.__dict__)
    with translate_db_errors(duplicate="An agent with this slug already exists."):
        session.add(agent)
        await session.flush()
        session.add_all(
            AgentTool(workspace_id=workspace_id, agent_id=agent.id, tool_key=key, enabled=True)
            for key in pack.tools
        )
        await session.flush()
    await _audit(session, actor, workspace_id, "agent.create", "agent", agent.id)
    return agent.id


async def _agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise NotFound("Agent not found.")
    return agent


async def update_agent(
    session: AsyncSession,
    actor: Actor,
    *,
    agent_id: uuid.UUID,
    row_version: int,
    changes: AgentChanges,
) -> None:
    require(actor, "agents:write")
    agent = await _agent(session, agent_id)
    if agent.row_version != row_version:
        raise Conflict("This agent was changed by someone else. Reload and try again.")
    updates = {
        f.name: getattr(changes, f.name) for f in fields(changes) if getattr(changes, f.name)
    }
    if updates.get("status") not in (None, "active", "disabled"):
        raise ValidationFailed(errors=[FieldError("status", "Unknown status.")])
    for name, value in updates.items():
        setattr(agent, name, value)
    agent.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(
        session,
        actor,
        agent.workspace_id,
        "agent.update",
        "agent",
        agent.id,
        {"fields": sorted(updates)},
    )


async def set_tool(
    session: AsyncSession,
    actor: Actor,
    *,
    agent_id: uuid.UUID,
    tool_key: str,
    enabled: bool,
    config: dict[str, Any] | None = None,
) -> None:
    """Turn one of the pack's tools on or off for an agent."""
    require(actor, "agents:write")
    agent = await _agent(session, agent_id)
    pack = await tenancy.installed_pack(session, agent.workspace_id)
    if tool_key not in pack.tools:
        raise ValidationFailed(errors=[FieldError("tool_key", "Not available in this pack.")])
    tool = await session.scalar(
        select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_key == tool_key)
    )
    if tool is None:
        tool = AgentTool(workspace_id=agent.workspace_id, agent_id=agent_id, tool_key=tool_key)
        session.add(tool)
    tool.enabled, tool.config, tool.updated_by = enabled, config, actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(
        session,
        actor,
        agent.workspace_id,
        "agent.tool",
        "agent",
        agent.id,
        {"tool": tool_key, "enabled": enabled},
    )


async def assign_phone_number(
    session: AsyncSession,
    actor: Actor,
    *,
    agent_id: uuid.UUID,
    phone_number: str,
    provider: str,
    trusted_trunk_id: str | None = None,
) -> uuid.UUID:
    """Route an inbound number to this agent. A number can be active for one agent only."""
    require(actor, "phone_numbers:manage")
    if not E164.fullmatch(phone_number):
        raise ValidationFailed(errors=[FieldError("phone_number", "Use E.164, e.g. +9180...")])
    agent = await _agent(session, agent_id)
    number = PhoneNumber(
        workspace_id=agent.workspace_id,
        agent_id=agent_id,
        phone_number=phone_number,
        provider=provider,
        trusted_trunk_id=trusted_trunk_id,
    )
    with translate_db_errors(duplicate="This number is already in use."):
        session.add(number)
        await session.flush()
    # The number itself is not logged: audit rows must not hold phone numbers.
    await _audit(
        session, actor, agent.workspace_id, "phone_number.assign", "phone_number", number.id
    )
    return number.id


async def release_phone_number(
    session: AsyncSession,
    actor: Actor,
    *,
    phone_number_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
) -> None:
    """Stop routing a number. With `agent_id`, the number must belong to that agent."""
    require(actor, "phone_numbers:manage")
    number = await session.get(PhoneNumber, phone_number_id)
    wrong_agent = agent_id is not None and number is not None and number.agent_id != agent_id
    if number is None or number.status == "released" or wrong_agent:
        raise NotFound("Phone number not found.")
    number.status = "released"
    with translate_db_errors():
        await session.flush()
    await _audit(
        session, actor, number.workspace_id, "phone_number.release", "phone_number", number.id
    )
