"""FastAPI dependencies shared by every router: paging and tenant-scoped database sessions."""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from praxima.shared.db.engine import tenant_transaction
from praxima.shared.db.pagination import DEFAULT_LIMIT, MAX_LIMIT, PageRequest
from praxima.shared.errors import Unavailable


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> PageRequest:
    return PageRequest(limit=limit, cursor=cursor)


Paging = Annotated[PageRequest, Depends(page_params)]


def session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The API's sessionmaker, created once at startup and stored on `app.state.sessions`."""
    sessions: async_sessionmaker[AsyncSession] | None = getattr(request.app.state, "sessions", None)
    if sessions is None:
        raise Unavailable("The database is not configured.")
    return sessions


async def workspace_session(
    workspace_id: uuid.UUID,
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
) -> AsyncIterator[AsyncSession]:
    """One transaction per request, scoped to the path's workspace for RLS.

    Routers must authorize the caller for `workspace_id` (membership check) before use;
    RLS is the second, database-level barrier.
    """
    async with tenant_transaction(sessions, workspace_id) as session:
        yield session


WorkspaceSession = Annotated[AsyncSession, Depends(workspace_session)]
