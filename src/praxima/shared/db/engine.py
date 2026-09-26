"""Async engine, sessions and scoped transactions. Never log URLs or parameters."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from praxima.shared.db.settings import ConfigurationError

_SET = text("SELECT set_config(:name, :value, true)")


@dataclass(frozen=True)
class Scope:
    """Who is acting and where. RLS policies read these as `app.*` settings.

    Only trusted plumbing (HTTP auth dependencies, the voice runtime, jobs) builds a Scope,
    after verifying the caller. Module code receives an already-scoped session.
    """

    user_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    # Set only while resolving a just-verified login (identity provider + subject).
    identity_provider: str | None = None
    identity_subject: str | None = None
    # Provider-verified email of that login (lowercased); lets an invited user be found.
    identity_email: str | None = None
    # Set only while authenticating a presented API key (SHA-256 hex of the key).
    api_key_hash: str | None = None

    def settings(self) -> list[tuple[str, str]]:
        values = {
            "app.workspace_id": self.workspace_id,
            "app.organization_id": self.organization_id,
            "app.user_id": self.user_id,
            "app.identity_provider": self.identity_provider,
            "app.identity_subject": self.identity_subject,
            "app.identity_email": self.identity_email,
            "app.api_key_hash": self.api_key_hash,
        }
        return [(name, str(value)) for name, value in values.items() if value is not None]


def create_engine(url: str, *, pool_size: int = 5) -> AsyncEngine:
    """Create the async engine for one database role (api, runtime or worker)."""
    if not url.startswith("postgresql+psycopg://"):
        raise ConfigurationError("Use a postgresql+psycopg:// URL for the application database.")
    return create_async_engine(url, pool_size=pool_size, pool_pre_ping=True, hide_parameters=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def scoped_transaction(
    sessions: async_sessionmaker[AsyncSession], scope: Scope
) -> AsyncIterator[AsyncSession]:
    """One short transaction whose RLS scope is `scope`.

    The settings are transaction-local (like SET LOCAL), so a pooled connection never
    carries one caller's scope into another request. Commits on success, rolls back on error.
    """
    async with sessions() as session, session.begin():
        for name, value in scope.settings():
            await session.execute(_SET, {"name": name, "value": value})
        yield session


@asynccontextmanager
async def tenant_transaction(
    sessions: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
) -> AsyncIterator[AsyncSession]:
    """Shorthand for a workspace-scoped transaction."""
    scope = Scope(workspace_id=workspace_id, organization_id=organization_id)
    async with scoped_transaction(sessions, scope) as session:
        yield session
