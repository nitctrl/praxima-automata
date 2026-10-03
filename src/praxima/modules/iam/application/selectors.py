"""Reads for identity and access. RLS limits every query to the caller's scope."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from praxima.modules.iam.domain.rules import Principal, effective_role
from praxima.modules.iam.infrastructure.models import Identity, Membership, PlatformAdmin, User
from praxima.shared.db.pagination import PageRequest, PageResult, fetch_page


@dataclass(frozen=True)
class MembershipView:
    id: uuid.UUID
    organization_id: uuid.UUID
    workspace_id: uuid.UUID | None
    role: str


@dataclass(frozen=True)
class MemberView:
    membership_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    status: str
    workspace_id: uuid.UUID | None
    created_at: datetime


async def principal_by_identity(
    session: AsyncSession, provider: str, subject: str
) -> tuple[Principal, str] | None:
    """The user linked to a verified login, with the user's status. One query."""
    row = (
        await session.execute(
            select(User.id, User.email, User.display_name, User.status)
            .join(Identity, Identity.user_id == User.id)
            .where(Identity.provider == provider, Identity.provider_subject == subject)
        )
    ).one_or_none()
    if row is None:
        return None
    return Principal(row.id, row.email, row.display_name), row.status


async def user_by_email(session: AsyncSession, email: str) -> User | None:
    """Only finds users visible in the current scope (e.g. the verified login email)."""
    return (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()


async def active_memberships(session: AsyncSession, user_id: uuid.UUID) -> list[MembershipView]:
    rows = await session.execute(
        select(Membership.id, Membership.organization_id, Membership.workspace_id, Membership.role)
        .where(Membership.user_id == user_id, Membership.status == "active")
        .order_by(Membership.created_at, Membership.id)
    )
    return [MembershipView(r.id, r.organization_id, r.workspace_id, r.role) for r in rows]


async def role_in(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
) -> str | None:
    """Effective role: the strongest active org-wide or matching workspace membership."""
    scope: ColumnElement[bool] = Membership.workspace_id.is_(None)
    if workspace_id is not None:
        scope = or_(scope, Membership.workspace_id == workspace_id)
    roles = await session.scalars(
        select(Membership.role).where(
            Membership.user_id == user_id,
            Membership.organization_id == organization_id,
            Membership.status == "active",
            scope,
        )
    )
    return effective_role(roles)


async def members_page(
    session: AsyncSession,
    organization_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    page: PageRequest,
) -> PageResult:
    """Members of an organization (workspace_id None) or one workspace, newest first.

    The user is joined in the same query: one round trip per page, no N+1.
    """
    statement = (
        select(Membership)
        .options(joinedload(Membership.user))
        .where(
            Membership.organization_id == organization_id,
            Membership.status != "revoked",
            Membership.workspace_id.is_(None)
            if workspace_id is None
            else Membership.workspace_id == workspace_id,
        )
    )
    result = await fetch_page(session, statement, (Membership.created_at, Membership.id), page)
    members = [
        MemberView(
            m.id,
            m.user.id,
            m.user.email,
            m.user.display_name,
            m.role,
            m.status,
            m.workspace_id,
            m.created_at,
        )
        for m in result.items
    ]
    return PageResult(members, result.next_cursor)


async def is_platform_admin(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """RLS lets a user see only their own platform_admins row."""
    return (
        await session.scalar(select(PlatformAdmin.user_id).where(PlatformAdmin.user_id == user_id))
    ) is not None


async def membership_id_for(
    session: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
) -> uuid.UUID | None:
    membership_id: uuid.UUID | None = await session.scalar(
        select(Membership.id).where(
            Membership.organization_id == organization_id,
            Membership.user_id == user_id,
            Membership.workspace_id.is_(None)
            if workspace_id is None
            else Membership.workspace_id == workspace_id,
            Membership.status != "revoked",
        )
    )
    return membership_id


@dataclass(frozen=True)
class PlatformAdminView:
    user_id: uuid.UUID
    email: str
    display_name: str | None
    granted_by: uuid.UUID | None
    granted_at: datetime


async def platform_admins(session: AsyncSession) -> list[PlatformAdminView]:
    """Every platform admin (a SECURITY DEFINER function: RLS shows each only their own row)."""
    rows = (
        await session.execute(
            text("SELECT user_id, granted_by, created_at FROM iam.admin_list_platform_admins()")
        )
    ).all()
    users = {
        u.id: u
        for u in await session.scalars(select(User).where(User.id.in_([r.user_id for r in rows])))
    }
    return [
        PlatformAdminView(
            r.user_id,
            users[r.user_id].email if r.user_id in users else "",
            users[r.user_id].display_name if r.user_id in users else None,
            r.granted_by,
            r.created_at,
        )
        for r in rows
    ]


async def member_counts(
    session: AsyncSession, organization_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """People with an active membership in each organization (one query)."""
    if not organization_ids:
        return {}
    rows = await session.execute(
        select(Membership.organization_id, func.count(func.distinct(Membership.user_id)))
        .where(Membership.organization_id.in_(organization_ids), Membership.status == "active")
        .group_by(Membership.organization_id)
    )
    return {organization_id: count for organization_id, count in rows.tuples()}


async def display_names(session: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Name (or email) of each user the scope may see, in one query."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(User.id, User.display_name, User.email).where(User.id.in_(set(user_ids)))
    )
    return {uid: name or email for uid, name, email in rows.tuples()}
