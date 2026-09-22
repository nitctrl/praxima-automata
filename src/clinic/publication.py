"""Verified staff-only preview/publish/rollback; no model-visible entrypoint.

The supplied connection must use authenticated with a server-verified auth.uid().
SQL reauthorizes, builds public data itself and checks optimistic preview tokens.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from clinic.snapshot import Snapshot
from clinic.staff import StaffRepository


@dataclass(frozen=True)
class Preview:
    snapshot: Snapshot
    digest: str
    active_version: UUID | None
    source_version: UUID | None


class ClinicConfigurationService:
    def __init__(self, connection: AsyncConnection[dict[str, Any]], clinic_id: UUID) -> None:
        self.connection = connection
        self.clinic_id = clinic_id

    async def preview(self, source_version: UUID | None = None) -> Preview:
        async with self.connection.transaction():
            await self.connection.execute("SET LOCAL statement_timeout = '10s'")
            await self.connection.execute("SET LOCAL lock_timeout = '3s'")
            await StaffRepository(self.connection, self.clinic_id)._authorize(write=True)
            row = await (
                await self.connection.execute(
                    "SELECT p.snapshot,p.digest,c.active_configuration_version_id "
                    "FROM clinic_private.preview_configuration(%s,%s) p "
                    "JOIN public.clinics c ON c.id=%s",
                    (self.clinic_id, source_version, self.clinic_id),
                )
            ).fetchone()
            if not row:
                raise ValueError("Preview unavailable")
            return Preview(
                Snapshot.model_validate(row["snapshot"]),
                row["digest"],
                row["active_configuration_version_id"],
                source_version,
            )

    async def publish(self, preview: Preview) -> UUID:
        if preview.snapshot.clinic_id != self.clinic_id:
            raise ValueError("Preview belongs to another clinic")
        async with self.connection.transaction():
            await self.connection.execute("SET LOCAL statement_timeout = '10s'")
            await self.connection.execute("SET LOCAL lock_timeout = '3s'")
            await StaffRepository(self.connection, self.clinic_id)._authorize(write=True)
            row = await (
                await self.connection.execute(
                    "SELECT * FROM clinic_private.publish_configuration(%s,%s,%s,%s)",
                    (
                        self.clinic_id,
                        preview.digest,
                        preview.active_version,
                        preview.source_version,
                    ),
                )
            ).fetchone()
            if not row:
                raise ValueError("Publication unavailable")
            # Validation failure rolls back the publication and audit, not just its response.
            Snapshot.model_validate(row["snapshot"])
            return UUID(str(row["version_id"]))

    async def rollback(self, preview: Preview) -> UUID:
        if preview.source_version is None:
            raise ValueError("Rollback requires a preview of an existing version")
        return await self.publish(preview)
