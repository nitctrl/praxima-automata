"""Tenancy tables: organizations, workspaces and the platform pack registry."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from praxima.shared.db.base import AuthoringMixin, Base, IdMixin, utc_now

SCHEMA = "tenancy"
SLUG = "^[a-z0-9][a-z0-9-]{1,62}$"


class Organization(IdMixin, AuthoringMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(f"slug ~ '{SLUG}'", name="slug_format"),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("status IN ('active', 'suspended', 'closed')", name="status"),
        {"schema": SCHEMA},
    )

    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    plan: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default=text("'active'"))
    data_region: Mapped[str | None] = mapped_column(Text)


class PackVersion(Base):
    """Platform table (no tenant): one row per released pack version. Owner-managed."""

    __tablename__ = "pack_versions"
    __table_args__ = (
        CheckConstraint("status IN ('available', 'deprecated', 'withdrawn')", name="status"),
        {"schema": SCHEMA},
    )

    pack_key: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, default="available", server_default=text("'available'")
    )
    released_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=func.now())


class Workspace(IdMixin, AuthoringMixin, Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug"),
        # Target of composite (organization_id, workspace_id) foreign keys.
        UniqueConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["pack_key", "pack_version"],
            [f"{SCHEMA}.pack_versions.pack_key", f"{SCHEMA}.pack_versions.version"],
        ),
        CheckConstraint(f"slug ~ '{SLUG}'", name="slug_format"),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("status IN ('active', 'suspended', 'archived')", name="status"),
        CheckConstraint(
            "cardinality(supported_languages) > 0 AND default_language = ANY (supported_languages)",
            name="languages",
        ),
        CheckConstraint("max_call_seconds IS NULL OR max_call_seconds > 0", name="max_call"),
        CheckConstraint(
            "monthly_minutes_limit IS NULL OR monthly_minutes_limit >= 0", name="minutes"
        ),
        {"schema": SCHEMA},
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.organizations.id"), index=True
    )
    slug: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    industry: Mapped[str] = mapped_column(Text)
    pack_key: Mapped[str] = mapped_column(Text)
    pack_version: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    default_language: Mapped[str] = mapped_column(Text)
    supported_languages: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[str] = mapped_column(Text, default="active", server_default=text("'active'"))
    max_call_seconds: Mapped[int | None]
    monthly_minutes_limit: Mapped[int | None]
    recording_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
