"""Agent releases: immutable, digest-checked snapshots of everything an agent may say.

One published release per agent (partial unique index). A published snapshot never changes
(trigger); publishing again creates a new version and supersedes the old one.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from praxima.shared.db.base import Base, IdMixin, TenantMixin, utc_now

SCHEMA = "releases"
WORKSPACES = "tenancy.workspaces.id"
USERS = "iam.users.id"


def _fk(columns: list[str], table: str) -> ForeignKeyConstraint:
    """Composite tenant key: a row can only reference a row of the same workspace."""
    return ForeignKeyConstraint(
        ["workspace_id", *columns], [f"{table}.workspace_id", f"{table}.id"]
    )


class AgentRelease(IdMixin, TenantMixin, Base):
    __tablename__ = "agent_releases"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("agent_id", "version_no"),
        _fk(["agent_id"], "agents.agents"),
        _fk(["source_release_id"], f"{SCHEMA}.agent_releases"),
        Index(
            "uq_agent_releases_one_published",
            "agent_id",
            unique=True,
            postgresql_where=sql_text("status = 'published'"),
        ),
        Index(None, "agent_id", "created_at", "id"),  # release history, newest first
        CheckConstraint(
            "status IN ('draft', 'published', 'superseded', 'archived')", name="status"
        ),
        CheckConstraint("digest ~ '^sha256:[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("version_no >= 1", name="version_no"),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot_object"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    agent_id: Mapped[uuid.UUID]
    version_no: Mapped[int]
    schema_version: Mapped[int]
    pack_key: Mapped[str] = mapped_column(Text)
    pack_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[int] = mapped_column(default=1, server_default=sql_text("1"))
    status: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(Text)
    # Item counts per section, so lists never load snapshots.
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_release_id: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(USERS))
    published_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(USERS))
    published_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=sql_text("now()"))
