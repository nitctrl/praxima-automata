"""Password login through the identity provider (Supabase Auth), then user resolution.

The provider token is used only to read the verified user and is never stored.
"""

from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from praxima.modules import iam
from praxima.shared.db.engine import Scope, apply_scope, scoped_transaction
from praxima.shared.errors import RateLimited, Unauthenticated, Unavailable

PROVIDER = "supabase"


class IdentityGateway(Protocol):
    async def call(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any: ...


async def verify_password(
    gateway: IdentityGateway, email: str, password: str
) -> iam.VerifiedIdentity:
    try:
        tokens = await gateway.call(
            "POST",
            "/auth/v1/token?grant_type=password",
            body={"email": email, "password": password},
        )
        user = await gateway.call("GET", "/auth/v1/user", token=tokens["access_token"])
        subject, verified_email = str(user["id"]), str(user["email"])
    except HTTPException as exc:
        if exc.status_code == 429:
            raise RateLimited() from None
        if exc.status_code >= 500:
            raise Unavailable("Sign-in is temporarily unavailable.") from None
        raise Unauthenticated("Email or password is incorrect.") from None
    except (KeyError, TypeError):
        raise Unavailable("Sign-in is temporarily unavailable.") from None
    confirmed = bool(user.get("email_confirmed_at") or user.get("confirmed_at"))
    return iam.VerifiedIdentity(PROVIDER, subject, verified_email, confirmed)


async def sign_in(
    sessions: async_sessionmaker[AsyncSession], identity: iam.VerifiedIdentity
) -> tuple[iam.Principal, list[iam.MembershipView]]:
    """Resolve or provision the user, then read their memberships, in one transaction."""
    login_scope = Scope(
        identity_provider=identity.provider,
        identity_subject=identity.subject,
        identity_email=identity.email.strip().lower() if identity.email_verified else None,
    )
    async with scoped_transaction(sessions, login_scope) as session:
        principal = await iam.login(session, identity)
        await apply_scope(session, Scope(user_id=principal.user_id))
        return principal, await iam.active_memberships(session, principal.user_id)
