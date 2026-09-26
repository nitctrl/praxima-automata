"""Write audit entries. Store field names and ids only, never values, PII or secrets."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules.audit.infrastructure.models import AuditEntry
from praxima.shared import context


async def record(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    actor_type: str = "user",
    actor_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    outcome: str = "success",
    change_diff: dict[str, Any] | None = None,
) -> None:
    """Append one entry in the caller's transaction, so it commits or rolls back with the change."""
    session.add(
        AuditEntry(
            organization_id=organization_id,
            workspace_id=workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            change_diff=change_diff,
            correlation_id=context.request_id.get(),
        )
    )
