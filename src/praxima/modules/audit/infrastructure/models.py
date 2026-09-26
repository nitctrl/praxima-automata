"""Append-only audit trail, partitioned monthly. Never store PII or secrets in it."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, PrimaryKeyConstraint, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from praxima.shared.db.base import Base, utc_now
from praxima.shared.kernel.ids import new_id

SCHEMA = "audit"


class AuditEntry(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        # Partitioned tables need the partition column in every unique key.
        PrimaryKeyConstraint("id", "occurred_at"),
        Index(None, "organization_id", "occurred_at"),
        Index(None, "workspace_id", "resource_type", "resource_id"),
        CheckConstraint(
            "actor_type IN ('user', 'api_key', 'system', 'runtime')", name="actor_type"
        ),
        CheckConstraint("outcome IN ('success', 'denied', 'failed')", name="outcome"),
        {"schema": SCHEMA, "postgresql_partition_by": "RANGE (occurred_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(default=new_id)
    # Client default: part of the primary key, so no RETURNING is needed under RLS.
    occurred_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=func.now())
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.organizations.id"))
    workspace_id: Mapped[uuid.UUID | None]
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None]
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[uuid.UUID | None]
    outcome: Mapped[str] = mapped_column(Text)
    change_diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    correlation_id: Mapped[str | None] = mapped_column(Text)
    hash_chain: Mapped[str | None] = mapped_column(Text)
