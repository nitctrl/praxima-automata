"""Password login through the identity provider (Supabase Auth), then user resolution.

The provider token is used only to read the verified user and is never stored.
"""

from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from praxima.modules import iam
from praxima.shared.db.engine import Scope, apply_scope, scoped_transaction
from praxima.shared.errors import RateLimited, Unauthenticated, Unavailable, ValidationFailed

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
    return _identity(user, subject, verified_email)


def _identity(user: dict[str, Any], subject: str, email: str) -> iam.VerifiedIdentity:
    confirmed = bool(user.get("email_confirmed_at") or user.get("confirmed_at"))
    metadata = user.get("user_metadata") or {}
    name = metadata.get("display_name") if isinstance(metadata, dict) else None
    display_name = (name.strip()[:200] or None) if isinstance(name, str) else None
    return iam.VerifiedIdentity(PROVIDER, subject, email, confirmed, display_name)


async def register(
    gateway: IdentityGateway, email: str, password: str, display_name: str
) -> iam.VerifiedIdentity | None:
    """Create the account with the identity provider.

    Returns the verified identity when the provider signs the new user in straight away, or
    None when they must confirm their email first. The same vague error covers "already
    registered" and "password rejected", so this can't be used to discover accounts.
    """
    try:
        result = await gateway.call(
            "POST",
            "/auth/v1/signup",
            body={"email": email, "password": password, "data": {"display_name": display_name}},
        )
    except HTTPException as exc:
        if exc.status_code == 429:
            raise RateLimited() from None
        if exc.status_code >= 500:
            raise Unavailable("Registration is temporarily unavailable.") from None
        raise ValidationFailed(
            "We couldn't create an account with these details. "
            "If you already have one, sign in instead."
        ) from None
    if not isinstance(result, dict) or not result.get("access_token"):
        return None  # the provider sent a confirmation email
    try:
        user = result.get("user") or await gateway.call(
            "GET", "/auth/v1/user", token=str(result["access_token"])
        )
        return _identity(user, str(user["id"]), str(user["email"]))
    except (HTTPException, KeyError, TypeError):
        raise Unavailable("Registration is temporarily unavailable.") from None


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
