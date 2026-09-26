"""Async engine, sessions and tenant-scoped transactions. Never log URLs or parameters."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from praxima.shared.db.settings import ConfigurationError

_SET_WORKSPACE = text("SELECT set_config('app.workspace_id', :value, true)")
_SET_ORGANIZATION = text("SELECT set_config('app.organization_id', :value, true)")


def create_engine(url: str, *, pool_size: int = 5) -> AsyncEngine:
    """Create the async engine for one database role (api, runtime or worker)."""
    if not url.startswith("postgresql+psycopg://"):
        raise ConfigurationError("Use a postgresql+psycopg:// URL for the application database.")
    return create_async_engine(url, pool_size=pool_size, pool_pre_ping=True, hide_parameters=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_transaction(
    sessions: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
) -> AsyncIterator[AsyncSession]:
    """One short transaction scoped to a tenant; RLS policies read these settings.

    The settings are transaction-local (like SET LOCAL), so a pooled connection never
    carries one tenant's scope into another request. Commits on success, rolls back on error.
    """
    async with sessions() as session, session.begin():
        await session.execute(_SET_WORKSPACE, {"value": str(workspace_id)})
        if organization_id is not None:
            await session.execute(_SET_ORGANIZATION, {"value": str(organization_id)})
        yield session
