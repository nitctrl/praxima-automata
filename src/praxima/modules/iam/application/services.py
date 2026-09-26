"""Writes for identity and access: login provisioning and membership changes."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import audit
from praxima.modules.iam.application.selectors import (
    membership_id_for,
    principal_by_identity,
    user_by_email,
)
from praxima.modules.iam.domain.rules import (
    ORG_WIDE_ROLES,
    ROLE_RANK,
    Actor,
    Principal,
    require,
    require_can_grant,
)
from praxima.modules.iam.infrastructure.models import Identity, Membership, User
from praxima.shared.db.base import utc_now
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import NotFound, PermissionDenied, ValidationFailed


@dataclass(frozen=True)
class VerifiedIdentity:
    """A login the identity provider has already verified (never raw client input)."""

    provider: str
    subject: str
    email: str
    email_verified: bool
    display_name: str | None = None


async def login(session: AsyncSession, identity: VerifiedIdentity) -> Principal:
    """Resolve or provision the user behind a verified login.

    Needs a scope carrying the identity (provider, subject and, when verified, email).
    """
    found = await principal_by_identity(session, identity.provider, identity.subject)
    if found is not None:
        principal, status = found
        if status != "active":
            raise PermissionDenied("This account is disabled.")
        await _touch_identity(session, identity)
        return principal

    email = identity.email.strip().lower()
    # An invited user already exists: link this login to it, but only for a verified email.
    user = await user_by_email(session, email) if identity.email_verified else None
    if user is not None and user.status != "active":
        raise PermissionDenied("This account is disabled.")
    with translate_db_errors(duplicate="This email is already registered."):
        if user is None:
            user = User(email=email, display_name=identity.display_name)
            session.add(user)
            await session.flush()
        session.add(
            Identity(
                user_id=user.id,
                provider=identity.provider,
                provider_subject=identity.subject,
                last_login_at=utc_now(),
            )
        )
        await session.flush()
    return Principal(user.id, user.email, user.display_name)


async def _touch_identity(session: AsyncSession, identity: VerifiedIdentity) -> None:
    row = (
        await session.execute(
            select(Identity).where(
                Identity.provider == identity.provider,
                Identity.provider_subject == identity.subject,
            )
        )
    ).scalar_one()
    row.last_login_at = utc_now()
    await session.flush()


async def set_membership(
    session: AsyncSession,
    actor: Actor,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    workspace_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Grant or change a role (org-wide when workspace_id is None). Idempotent."""
    require(actor, "members:manage")
    if role not in ROLE_RANK:
        raise ValidationFailed("Unknown role.")
    if role in ORG_WIDE_ROLES and workspace_id is not None:
        raise ValidationFailed("Owners and admins are organization-wide.")
    require_can_grant(actor, role)
    existing = (
        await session.execute(
            select(Membership).where(
                Membership.user_id == user_id,
                Membership.organization_id == organization_id,
                Membership.workspace_id.is_(None)
                if workspace_id is None
                else Membership.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    with translate_db_errors(reference="That user or workspace doesn't exist."):
        if existing is None:
            existing = Membership(
                user_id=user_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                role=role,
                created_by=actor.user_id,
            )
            session.add(existing)
        else:
            if existing.role == "owner" and role != "owner":
                require_can_grant(actor, "owner")  # only owners demote owners
            existing.role, existing.status = role, "active"
        await session.flush()
    await audit.record(
        session,
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_id=actor.user_id,
        action="membership.set",
        resource_type="membership",
        resource_id=existing.id,
        change_diff={"fields": ["role", "status"]},
    )
    return existing.id


async def revoke_membership(
    session: AsyncSession, actor: Actor, *, organization_id: uuid.UUID, membership_id: uuid.UUID
) -> None:
    require(actor, "members:manage")
    membership = await session.get(Membership, membership_id)
    if membership is None or membership.organization_id != organization_id:
        raise NotFound("Membership not found.")
    require_can_grant(actor, membership.role)
    if membership.user_id == actor.user_id and membership.role == "owner":
        raise ValidationFailed("Owners can't remove their own ownership.")
    membership.status = "revoked"
    with translate_db_errors():
        await session.flush()
    await audit.record(
        session,
        organization_id=organization_id,
        workspace_id=membership.workspace_id,
        actor_id=actor.user_id,
        action="membership.revoke",
        resource_type="membership",
        resource_id=membership.id,
    )


async def revoke_membership_of(
    session: AsyncSession,
    actor: Actor,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
) -> None:
    """Revoke a user's org-wide (workspace_id None) or workspace membership."""
    membership_id = await membership_id_for(session, organization_id, user_id, workspace_id)
    if membership_id is None:
        raise NotFound("Membership not found.")
    await revoke_membership(
        session, actor, organization_id=organization_id, membership_id=membership_id
    )
