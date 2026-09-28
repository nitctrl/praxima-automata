"""Reads for agent releases. Lists never load snapshots (they carry per-section counts)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from praxima.modules.releases.infrastructure.models import AgentRelease
from praxima.shared.db.pagination import PageRequest, PageResult, fetch_page
from praxima.shared.errors import NotFound


@dataclass(frozen=True)
class ReleaseView:
    id: uuid.UUID
    agent_id: uuid.UUID
    version_no: int
    status: str
    schema_version: int
    pack_key: str
    pack_version: str
    digest: str
    summary: dict[str, Any]
    source_release_id: uuid.UUID | None
    published_by: uuid.UUID | None
    published_at: datetime | None
    created_at: datetime


def _view(r: AgentRelease) -> ReleaseView:
    return ReleaseView(
        r.id,
        r.agent_id,
        r.version_no,
        r.status,
        r.schema_version,
        r.pack_key,
        r.pack_version,
        r.digest,
        r.summary,
        r.source_release_id,
        r.published_by,
        r.published_at,
        r.created_at,
    )


async def releases_page(
    session: AsyncSession, page: PageRequest, *, agent_id: uuid.UUID
) -> PageResult:
    """Newest first. Snapshot columns are deferred: a list is one light query."""
    statement = (
        select(AgentRelease)
        .options(defer(AgentRelease.snapshot, raiseload=True))
        .where(AgentRelease.agent_id == agent_id)
    )
    result = await fetch_page(session, statement, (AgentRelease.created_at, AgentRelease.id), page)
    return PageResult([_view(r) for r in result.items], result.next_cursor)


async def get_release(
    session: AsyncSession, release_id: uuid.UUID, *, agent_id: uuid.UUID
) -> tuple[ReleaseView, dict[str, Any]]:
    release = await session.get(AgentRelease, release_id)
    if release is None or release.agent_id != agent_id:
        raise NotFound("Release not found.")
    return _view(release), release.snapshot


async def live_release(session: AsyncSession, agent_id: uuid.UUID) -> ReleaseView | None:
    release = await session.scalar(
        select(AgentRelease)
        .options(defer(AgentRelease.snapshot, raiseload=True))
        .where(AgentRelease.agent_id == agent_id, AgentRelease.status == "published")
    )
    return _view(release) if release else None
