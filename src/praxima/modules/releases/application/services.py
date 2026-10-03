"""Build, preview, publish and roll back agent releases.

Preview builds the snapshot from published content and returns its digest; nothing is stored.
Publish rebuilds and stores it only if the digest still matches (someone may have changed
content since the preview). Rollback republishes an earlier snapshot as a new version.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.contracts.agent_snapshot import (
    MAX_SNAPSHOT_BYTES,
    SCHEMA_VERSION,
    AgentSnapshot,
    canonical_json,
    diff,
    digest_of,
    summarize,
)
from praxima.modules import agents, audit, catalog, engagement, knowledge, tenancy
from praxima.modules.iam import Actor, require
from praxima.modules.releases.infrastructure.models import AgentRelease
from praxima.shared.db.base import utc_now
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import Conflict, NotFound, ValidationFailed

PROMPT_VERSION = 1


@dataclass(frozen=True)
class Preview:
    digest: str
    summary: dict[str, Any]
    changes: dict[str, Any]
    warnings: list[str]
    live_version_no: int | None
    unchanged: bool


async def build_snapshot(
    session: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID, at: datetime
) -> dict[str, Any]:
    """Everything published for this agent, validated, in canonical form."""
    workspace = await tenancy.get_workspace(session, workspace_id)
    pack = await tenancy.installed_pack(session, workspace_id)
    agent = await agents.get_agent(session, agent_id)
    published = await catalog.published_catalog(session, at)
    content = await knowledge.published_knowledge(session, at)
    kinds = await engagement.work_item_kinds(session)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "pack": {
            "key": workspace.pack_key,
            "version": workspace.pack_version,
            "callback_kind": pack.callback_kind,
        },
        "workspace": {
            "name": workspace.name,
            "timezone": workspace.timezone,
            "default_language": workspace.default_language,
            "supported_languages": list(workspace.supported_languages),
        },
        "agent": {
            "id": str(agent.id),
            "name": agent.name,
            "persona": agent.persona,
            "greeting": agent.greeting_message,
            "emergency_message": agent.emergency_message,
            "fallback_message": agent.fallback_message,
            "transfer_enabled": agent.transfer_enabled,
            "prompt_version": PROMPT_VERSION,
        },
        "tools": [{"key": t.key} for t in sorted(agent.tools, key=lambda t: t.key) if t.enabled],
        "entity_types": published.entity_types,
        "entities": published.entities,
        "relations": published.relations,
        "availability": {"rules": published.rules, "exceptions": published.exceptions},
        "work_item_kinds": [
            {
                "key": k.key,
                "name": k.name,
                "schema_version": k.schema_version,
                "payload_schema": k.payload_schema,
                "stages": k.stages,
                "initial_stage": k.initial_stage,
                "terminal_stages": k.terminal_stages,
                "subject_types": k.subject_types,
            }
            for k in kinds
        ],
        "faqs": content.faqs,
        "knowledge_sections": content.sections,
        "announcements": content.announcements,
    }
    try:
        validated = AgentSnapshot.model_validate(snapshot).model_dump(mode="json")
    except ValidationError:
        # Published content should always fit; if not, it's a bug, never a partial release.
        raise ValidationFailed("The published content could not be packaged.") from None
    if len(canonical_json(validated).encode()) > MAX_SNAPSHOT_BYTES:
        raise ValidationFailed("The published content is too large for one release (5 MB).")
    return validated


def _warnings(snapshot: dict[str, Any], agent: agents.AgentView) -> list[str]:
    found = []
    if agent.status != "active":
        found.append("This agent is disabled, so it won't answer calls until it's enabled.")
    if not any(n.status == "active" for n in agent.phone_numbers):
        found.append("No phone number is routed to this agent yet.")
    if not (snapshot["entities"] or snapshot["knowledge_sections"] or snapshot["faqs"]):
        found.append(
            "Nothing is published yet: no directory entries, documents or approved answers."
        )
    if not snapshot["tools"]:
        found.append("All tools are switched off, so the agent can only talk.")
    return found


async def _live(session: AsyncSession, agent_id: uuid.UUID) -> AgentRelease | None:
    """The live release, locked so two publishes of one agent can't interleave."""
    live: AgentRelease | None = await session.scalar(
        select(AgentRelease)
        .where(AgentRelease.agent_id == agent_id, AgentRelease.status == "published")
        .with_for_update()
    )
    return live


async def preview_release(
    session: AsyncSession, actor: Actor, *, workspace_id: uuid.UUID, agent_id: uuid.UUID
) -> Preview:
    """What would be published now, and how it differs from the live release."""
    require(actor, "releases:read")
    snapshot = await build_snapshot(session, workspace_id, agent_id, utc_now())
    live = await _live(session, agent_id)
    digest = digest_of(snapshot)
    agent = await agents.get_agent(session, agent_id)
    return Preview(
        digest=digest,
        summary=summarize(snapshot),
        changes=diff(live.snapshot if live else None, snapshot),
        warnings=_warnings(snapshot, agent),
        live_version_no=live.version_no if live else None,
        unchanged=live is not None and live.digest == digest,
    )


async def _store(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    snapshot: dict[str, Any],
    digest: str,
    live: AgentRelease | None,
    source_release_id: uuid.UUID | None = None,
) -> uuid.UUID:
    last = await session.scalar(
        select(func.max(AgentRelease.version_no)).where(AgentRelease.agent_id == agent_id)
    )
    now = utc_now()
    release = AgentRelease(
        workspace_id=workspace_id,
        agent_id=agent_id,
        version_no=(last or 0) + 1,
        schema_version=snapshot["schema_version"],
        pack_key=snapshot["pack"]["key"],
        pack_version=snapshot["pack"]["version"],
        prompt_version=snapshot["agent"]["prompt_version"],
        status="published",
        snapshot=snapshot,
        digest=digest,
        summary=summarize(snapshot),
        source_release_id=source_release_id,
        created_by=actor.user_id,
        published_by=actor.user_id,
        published_at=now,
    )
    with translate_db_errors(duplicate="Someone published this agent at the same time."):
        if live is not None:
            await session.execute(
                update(AgentRelease)
                .where(AgentRelease.id == live.id)
                .values(status="superseded")
                .execution_options(synchronize_session=False)
            )
        session.add(release)
        await session.flush()
    await audit.record(
        session,
        organization_id=await tenancy.organization_of(session, workspace_id),
        workspace_id=workspace_id,
        actor_id=actor.user_id,
        action="release.rollback" if source_release_id else "release.publish",
        resource_type="agent_release",
        resource_id=release.id,
        change_diff={"version_no": release.version_no, "digest": digest},
    )
    return release.id


async def publish_release(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    digest: str,
) -> uuid.UUID:
    """Publish exactly what was previewed: 409 if published content changed since then."""
    require(actor, "releases:publish")
    live = await _live(session, agent_id)
    snapshot = await build_snapshot(session, workspace_id, agent_id, utc_now())
    current = digest_of(snapshot)
    if current != digest:
        raise Conflict("Published content changed since your preview. Preview again.")
    if live is not None and live.digest == current:
        raise Conflict("Nothing has changed since the live release.")
    return await _store(
        session,
        actor,
        workspace_id=workspace_id,
        agent_id=agent_id,
        snapshot=snapshot,
        digest=current,
        live=live,
    )


async def rollback_release(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    source_release_id: uuid.UUID,
) -> uuid.UUID:
    """Make an earlier release live again, as a new version pointing at its source."""
    require(actor, "releases:publish")
    source = await session.get(AgentRelease, source_release_id)
    if source is None or source.agent_id != agent_id:
        raise NotFound("Release not found.")
    live = await _live(session, agent_id)
    if live is not None and live.digest == source.digest:
        raise Conflict("That content is already live.")
    return await _store(
        session,
        actor,
        workspace_id=workspace_id,
        agent_id=agent_id,
        snapshot=source.snapshot,
        digest=source.digest,
        live=live,
        source_release_id=source.id,
    )
