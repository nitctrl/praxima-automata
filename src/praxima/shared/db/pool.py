"""Bounded async connections for the restricted voice runtime, not migrations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from praxima.shared.db.settings import ConfigurationError, DatabaseSettings


class RuntimeDatabase:
    def __init__(self, settings: DatabaseSettings) -> None:
        self._pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            settings.dsn,
            open=False,
            min_size=0,
            max_size=4,
            max_waiting=16,
            # Allow the configured ten-second connection attempt to finish before
            # declaring the pool unavailable on a cold Supabase connection.
            timeout=12,
            kwargs={"row_factory": dict_row, "connect_timeout": 10},
        )

    async def open(self) -> None:
        await self._pool.open()
        try:
            async with self.connection():
                pass
        except Exception:
            await self._pool.close()
            raise

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(
        self, clinic_id: UUID | None = None
    ) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.execute("SET LOCAL lock_timeout = '2s'")
            role = await (
                await conn.execute(
                    "SELECT current_user = 'clinic_runtime' "
                    "AND session_user = 'clinic_runtime' AS correct, "
                    "rolsuper OR rolbypassrls AS privileged "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            ).fetchone()
            if not role or not role["correct"] or role["privileged"]:
                raise ConfigurationError("Runtime requires the restricted clinic_runtime role.")
            # Only backend-derived scope goes here; never a model or browser field.
            await conn.execute(
                "SELECT set_config('app.clinic_id', %s, true)",
                (str(clinic_id) if clinic_id else "",),
            )
            yield conn
