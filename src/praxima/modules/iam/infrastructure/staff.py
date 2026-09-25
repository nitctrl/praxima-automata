"""Staff repository for a future verified Supabase Auth request adapter.

This does not authenticate JWTs. The connection must already carry a verified
Supabase auth.uid() context, and execute as authenticated (never service_role).
No HTTP endpoint or caller tool exposes this interface in Phase 1.
"""

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection


class Forbidden(PermissionError):
    """Safe authorization failure."""


class StaffRepository:
    def __init__(self, connection: AsyncConnection[dict[str, Any]], clinic_id: UUID) -> None:
        self.connection = connection
        self.clinic_id = clinic_id

    async def _authorize(self, *, write: bool = False) -> None:
        roles = ["owner", "manager"] if write else ["owner", "manager", "receptionist", "viewer"]
        row = await (
            await self.connection.execute(
                "SELECT current_user = 'authenticated' AND "
                "clinic_private.has_membership(%s, %s) AS allowed",
                (self.clinic_id, roles),
            )
        ).fetchone()
        if not row or not row["allowed"]:
            raise Forbidden("Active clinic membership with the required role is necessary.")

    async def list_doctors(self) -> list[dict[str, Any]]:
        await self._authorize()
        return await (
            await self.connection.execute(
                "SELECT id, display_name, speciality, status FROM public.doctors "
                "WHERE clinic_id = %s ORDER BY normalized_name, id LIMIT 100",
                (self.clinic_id,),
            )
        ).fetchall()

    async def deactivate_schedule(self, schedule_id: UUID) -> bool:
        await self._authorize(write=True)
        cursor = await self.connection.execute(
            "UPDATE public.weekly_schedules SET status = 'inactive' "
            "WHERE clinic_id = %s AND id = %s RETURNING id",
            (self.clinic_id, schedule_id),
        )
        return await cursor.fetchone() is not None
