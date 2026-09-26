"""Identity and access tables. Passwords are never stored: login is the identity provider's."""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from praxima.shared.db.base import Base, IdMixin, TimestampMixin, utc_now

SCHEMA = "iam"
ROLES = ("owner", "admin", "manager", "staff", "viewer")
WORKSPACE_FK = (
    ["organization_id", "workspace_id"],
    ["tenancy.workspaces.organization_id", "tenancy.workspaces.id"],
)


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Emails are stored lowercased (services normalize), so plain text stays portable.
        CheckConstraint(
            "email = lower(email) AND length(email) BETWEEN 3 AND 320 "
            "AND position('@' in email) > 1",
            name="email_format",
        ),
        CheckConstraint("display_name IS NULL OR length(display_name) <= 200", name="name_length"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        {"schema": SCHEMA},
    )

    email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default=text("'active'"))

    identities: Mapped[list["Identity"]] = relationship(back_populates="user", lazy="raise")


class Identity(IdMixin, Base):
    """Links a user to an external login (e.g. provider 'supabase', subject = its user id)."""

    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("provider", "provider_subject"), {"schema": SCHEMA})

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.users.id"), index=True)
    provider: Mapped[str] = mapped_column(Text)
    provider_subject: Mapped[str] = mapped_column(Text)
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="identities", lazy="raise")


class Membership(IdMixin, TimestampMixin, Base):
    """A user's role in an organization (workspace_id NULL) or in one workspace."""

    __tablename__ = "memberships"
    __table_args__ = (
        ForeignKeyConstraint(*WORKSPACE_FK),
        Index(
            None,
            "user_id",
            "organization_id",
            "workspace_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(f"role IN {ROLES}", name="role"),
        CheckConstraint("status IN ('invited', 'active', 'suspended', 'revoked')", name="status"),
        # Owners and admins act on the whole organization.
        CheckConstraint(
            "role NOT IN ('owner', 'admin') OR workspace_id IS NULL", name="org_wide_roles"
        ),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.users.id"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenancy.organizations.id"), index=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default=text("'active'"))
    created_by: Mapped[uuid.UUID | None]

    user: Mapped[User] = relationship(lazy="raise")


class ApiKey(IdMixin, Base):
    """Machine credential for integrations. Only a SHA-256 hash of the key is stored."""

    __tablename__ = "api_keys"
    __table_args__ = (
        ForeignKeyConstraint(*WORKSPACE_FK),
        CheckConstraint("length(name) BETWEEN 1 AND 150", name="name_length"),
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="key_hash_format"),
        {"schema": SCHEMA},
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenancy.organizations.id"), index=True
    )
    workspace_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(Text)
    key_prefix: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=func.now())


class PlatformAdmin(Base):
    """Platform operators. Owner-managed: the application can only read its own row."""

    __tablename__ = "platform_admins"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.users.id"), primary_key=True)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(f"{SCHEMA}.users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=func.now())
