"""ORM base and standard columns. Models live only in each module's infrastructure layer."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from praxima.shared.kernel.ids import new_id

# One Postgres schema per module (docs/database/database-schema.md §5).
MODULE_SCHEMAS = (
    "iam",
    "tenancy",
    "agents",
    "releases",
    "catalog",
    "knowledge",
    "engagement",
    "billing",
    "audit",
    "ops",
)

# Deterministic constraint names so Alembic diffs stay stable across environments.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)


class TenantMixin:
    """Tenant-owned row. RLS and composite tenant foreign keys key on `workspace_id`."""

    workspace_id: Mapped[uuid.UUID]


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class AuthoringMixin(TimestampMixin):
    """Staff-editable row: audit columns, soft delete and optimistic concurrency (HTTP 409)."""

    created_by: Mapped[uuid.UUID | None]
    updated_by: Mapped[uuid.UUID | None]
    deleted_at: Mapped[datetime | None]
    row_version: Mapped[int] = mapped_column(server_default=text("1"))

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        # A stale UPDATE matches no row and raises StaleDataError instead of overwriting.
        return {"version_id_col": cls.row_version}
