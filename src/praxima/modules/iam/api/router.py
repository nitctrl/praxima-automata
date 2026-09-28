"""Sign-in session and membership endpoints. No business logic or queries here."""

import uuid

from fastapi import APIRouter, Request, Response, status

from praxima.entrypoints.http.deps import (
    CurrentUser,
    Gateway,
    LoginLimiter,
    OrganizationAccess,
    Paging,
    SecureCookies,
    SelfSignup,
    Sessions,
    UserSession,
    WebSessions,
    WorkspaceAccess,
)
from praxima.entrypoints.http.identity import register, sign_in, verify_password
from praxima.entrypoints.http.responses import Page
from praxima.entrypoints.http.sessions import COOKIE_NAME
from praxima.modules import iam
from praxima.modules.iam.api.schemas import (
    GrantOut,
    MemberOut,
    MembershipOut,
    OrganizationRoleIn,
    Registration,
    RegistrationOut,
    SessionOut,
    SignIn,
    UserOut,
    WorkspaceRoleIn,
)
from praxima.shared.errors import PermissionDenied

router = APIRouter()


def _session_out(
    csrf: str, principal: iam.Principal, memberships: list[iam.MembershipView]
) -> SessionOut:
    return SessionOut(
        csrf=csrf,
        user=UserOut(
            id=principal.user_id, email=principal.email, display_name=principal.display_name
        ),
        memberships=[MembershipOut.of(m) for m in memberships],
    )


def _start_session(
    request: Request,
    response: Response,
    store: WebSessions,
    secure: bool,
    principal: iam.Principal,
    memberships: list[iam.MembershipView],
) -> SessionOut:
    store.delete(request.cookies.get(COOKIE_NAME, ""))  # no session fixation
    sid, web = store.create(principal.user_id, principal.email, principal.display_name)
    response.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=store.ttl,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/api",
    )
    return _session_out(web.csrf, principal, memberships)


@router.post("/auth/session", tags=["auth"])
async def create_session(
    body: SignIn,
    request: Request,
    response: Response,
    gateway: Gateway,
    sessions: Sessions,
    store: WebSessions,
    limiter: LoginLimiter,
    secure: SecureCookies,
) -> SessionOut:
    """Sign in with email and password. Sets an HttpOnly session cookie."""
    limiter.hit(request.client.host if request.client else "unknown")
    identity = await verify_password(gateway, body.email, body.password)
    principal, memberships = await sign_in(sessions, identity)
    return _start_session(request, response, store, secure, principal, memberships)


@router.post("/auth/registrations", tags=["auth"], status_code=status.HTTP_201_CREATED)
async def create_registration(
    body: Registration,
    request: Request,
    response: Response,
    gateway: Gateway,
    sessions: Sessions,
    store: WebSessions,
    limiter: LoginLimiter,
    secure: SecureCookies,
    allowed: SelfSignup,
) -> RegistrationOut:
    """Create an account (only when self-service sign-up is enabled).

    Signs the new user in when the identity provider allows it straight away; otherwise
    they confirm their email, then sign in.
    """
    if not allowed:
        raise PermissionDenied("Self-service sign-up is turned off. Ask an administrator.")
    limiter.hit(request.client.host if request.client else "unknown")
    identity = await register(gateway, body.email, body.password, body.display_name.strip())
    if identity is None:
        return RegistrationOut(status="confirmation_required")
    principal, memberships = await sign_in(sessions, identity)
    return RegistrationOut(
        status="signed_in",
        session=_start_session(request, response, store, secure, principal, memberships),
    )


@router.get("/auth/session", tags=["auth"])
async def read_session(user: CurrentUser, session: UserSession) -> SessionOut:
    """The current user and memberships (restores a session after a page reload)."""
    memberships = await iam.active_memberships(session, user.user_id)
    principal = iam.Principal(user.user_id, user.email, user.display_name)
    return _session_out(user.csrf, principal, memberships)


@router.delete("/auth/session", tags=["auth"], status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(request: Request, user: CurrentUser, store: WebSessions) -> Response:
    store.delete(request.cookies.get(COOKIE_NAME, ""))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(COOKIE_NAME, path="/api")
    return response


@router.get("/workspaces/{workspace_id}/members", tags=["members"])
async def list_workspace_members(access: WorkspaceAccess, page: Paging) -> Page[MemberOut]:
    iam.require(access.actor, "members:read")
    result = await iam.members_page(
        access.session, access.organization_id, access.workspace_id, page
    )
    return Page.build(result, [MemberOut.of(m) for m in result.items], page.limit)


@router.put("/workspaces/{workspace_id}/memberships/{user_id}", tags=["members"])
async def set_workspace_role(
    user_id: uuid.UUID, body: WorkspaceRoleIn, access: WorkspaceAccess
) -> GrantOut:
    membership_id = await iam.set_membership(
        access.session,
        access.actor,
        organization_id=access.organization_id,
        user_id=user_id,
        role=body.role,
        workspace_id=access.workspace_id,
    )
    return GrantOut(
        membership_id=membership_id,
        user_id=user_id,
        organization_id=access.organization_id,
        workspace_id=access.workspace_id,
        role=body.role,
    )


@router.delete(
    "/workspaces/{workspace_id}/memberships/{user_id}",
    tags=["members"],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_workspace_role(user_id: uuid.UUID, access: WorkspaceAccess) -> None:
    await iam.revoke_membership_of(
        access.session,
        access.actor,
        organization_id=access.organization_id,
        user_id=user_id,
        workspace_id=access.workspace_id,
    )


@router.get("/organizations/{organization_id}/members", tags=["members"])
async def list_organization_members(access: OrganizationAccess, page: Paging) -> Page[MemberOut]:
    iam.require(access.actor, "members:read")
    result = await iam.members_page(access.session, access.organization_id, None, page)
    return Page.build(result, [MemberOut.of(m) for m in result.items], page.limit)


@router.put("/organizations/{organization_id}/memberships/{user_id}", tags=["members"])
async def set_organization_role(
    user_id: uuid.UUID, body: OrganizationRoleIn, access: OrganizationAccess
) -> GrantOut:
    membership_id = await iam.set_membership(
        access.session,
        access.actor,
        organization_id=access.organization_id,
        user_id=user_id,
        role=body.role,
    )
    return GrantOut(
        membership_id=membership_id,
        user_id=user_id,
        organization_id=access.organization_id,
        workspace_id=None,
        role=body.role,
    )


@router.delete(
    "/organizations/{organization_id}/memberships/{user_id}",
    tags=["members"],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_organization_role(user_id: uuid.UUID, access: OrganizationAccess) -> None:
    await iam.revoke_membership_of(
        access.session, access.actor, organization_id=access.organization_id, user_id=user_id
    )
