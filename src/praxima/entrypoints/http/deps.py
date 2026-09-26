"""FastAPI dependencies shared by every router: auth, tenant-scoped sessions, paging.

Transaction dependencies use scope="function": they commit before the response is sent,
so a failed commit is reported instead of a success.
"""

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from praxima.entrypoints.http.identity import IdentityGateway
from praxima.entrypoints.http.sessions import COOKIE_NAME, RateLimiter, SessionStore, WebSession
from praxima.modules import iam, tenancy
from praxima.modules.engagement import Vault
from praxima.modules.knowledge import KnowledgeIndex
from praxima.shared.db.engine import Scope, apply_scope, scoped_transaction
from praxima.shared.db.pagination import DEFAULT_LIMIT, MAX_LIMIT, PageRequest
from praxima.shared.errors import NotFound, PermissionDenied, Unauthenticated, Unavailable

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SessionMaker = async_sessionmaker[AsyncSession]


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> PageRequest:
    return PageRequest(limit=limit, cursor=cursor)


Paging = Annotated[PageRequest, Depends(page_params)]


def _state(request: Request, name: str) -> object:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise Unavailable("This service is not configured.")
    return value


def session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The API's sessionmaker (app.state.sessions), created once at startup."""
    return _state(request, "sessions")  # type: ignore[return-value]


def web_sessions(request: Request) -> SessionStore:
    return _state(request, "web_sessions")  # type: ignore[return-value]


def identity_gateway(request: Request) -> IdentityGateway:
    return _state(request, "identity_gateway")  # type: ignore[return-value]


def login_limiter(request: Request) -> RateLimiter:
    return _state(request, "login_limiter")  # type: ignore[return-value]


def cookie_secure(request: Request) -> bool:
    """From the configured dashboard origin (https → Secure). Secure unless told otherwise."""
    return bool(getattr(request.app.state, "cookie_secure", True))


Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)]
SecureCookies = Annotated[bool, Depends(cookie_secure)]
WebSessions = Annotated[SessionStore, Depends(web_sessions)]
Gateway = Annotated[IdentityGateway, Depends(identity_gateway)]
LoginLimiter = Annotated[RateLimiter, Depends(login_limiter)]


def current_user(request: Request, store: WebSessions) -> WebSession:
    """The signed-in browser session; state-changing requests must carry its CSRF token."""
    session = store.get(request.cookies.get(COOKIE_NAME, ""))
    if session is None:
        raise Unauthenticated()
    if request.method not in SAFE_METHODS and not secrets.compare_digest(
        request.headers.get("x-csrf-token", ""), session.csrf
    ):
        raise PermissionDenied("CSRF verification failed.")
    return session


CurrentUser = Annotated[WebSession, Depends(current_user)]


async def user_session(user: CurrentUser, sessions: Sessions) -> AsyncIterator[AsyncSession]:
    """A transaction scoped to the signed-in user only (their own memberships)."""
    async with scoped_transaction(sessions, Scope(user_id=user.user_id)) as session:
        yield session


UserSession = Annotated[AsyncSession, Depends(user_session, scope="function")]


@dataclass(frozen=True)
class Access:
    """An authorized, tenant-scoped unit of work handed to one endpoint."""

    session: AsyncSession
    actor: iam.Actor
    organization_id: uuid.UUID
    workspace_id: uuid.UUID | None


async def _authorize(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    missing: str,
) -> Access:
    role = await iam.role_in(session, user_id, organization_id, workspace_id)
    admin = await iam.is_platform_admin(session, user_id)
    if role is None and not admin:
        raise NotFound(missing)  # never reveal that another tenant's resource exists
    await apply_scope(
        session, Scope(user_id=user_id, organization_id=organization_id, workspace_id=workspace_id)
    )
    return Access(session, iam.Actor(user_id, role, admin), organization_id, workspace_id)


async def workspace_access(
    workspace_id: uuid.UUID, user: CurrentUser, sessions: Sessions
) -> AsyncIterator[Access]:
    """Resolve the caller's role in the path's workspace, then scope RLS to it."""
    async with scoped_transaction(sessions, Scope(user_id=user.user_id)) as session:
        organization_id = await tenancy.organization_of(session, workspace_id)
        yield await _authorize(
            session, user.user_id, organization_id, workspace_id, "Workspace not found."
        )


async def organization_access(
    organization_id: uuid.UUID, user: CurrentUser, sessions: Sessions
) -> AsyncIterator[Access]:
    """Resolve the caller's organization-wide role, then scope RLS to the organization."""
    async with scoped_transaction(sessions, Scope(user_id=user.user_id)) as session:
        yield await _authorize(
            session, user.user_id, organization_id, None, "Organization not found."
        )


WorkspaceAccess = Annotated[Access, Depends(workspace_access, scope="function")]
OrganizationAccess = Annotated[Access, Depends(organization_access, scope="function")]


async def narrow_to_workspace(access: Access, workspace_id: uuid.UUID) -> Access:
    """Scope an organization-level unit of work to one of its workspaces (e.g. just created).

    The caller's organization role carries over: owners and admins act on every workspace.
    """
    await apply_scope(
        access.session,
        Scope(
            user_id=access.actor.user_id,
            organization_id=access.organization_id,
            workspace_id=workspace_id,
        ),
    )
    return Access(access.session, access.actor, access.organization_id, workspace_id)


def knowledge_index(request: Request) -> KnowledgeIndex | None:
    """The Qdrant index when configured; None means keyword search only (never an error)."""
    return getattr(request.app.state, "knowledge_index", None)


Index = Annotated[KnowledgeIndex | None, Depends(knowledge_index)]


def pii_vault(request: Request) -> Vault:
    """Encryption and phone lookup keys for CRM personal data; 503 when not configured."""
    return _state(request, "vault")  # type: ignore[return-value]


PiiVault = Annotated[Vault, Depends(pii_vault)]
