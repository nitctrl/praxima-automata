"""Bounded async connections for the restricted voice runtime, not migrations."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from clinic.settings import ConfigurationError, DatabaseSettings


class RuntimeDatabase:
    """Connections for one call.

    LiveKit runs each call in its own process, so this pool lives exactly as long as the
    call. Keep it small and point ``DATABASE_URL`` at the Supabase pooler so Postgres
    connections scale with concurrent calls, not with worker processes.
    """

    def __init__(self, settings: DatabaseSettings) -> None:
        self._pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            settings.dsn,
            open=False,
            min_size=0,
            max_size=2,
            max_waiting=16,
            # Allow the configured ten-second connection attempt to finish before
            # declaring the pool unavailable on a cold Supabase connection.
            timeout=12,
            # No server-side prepared statements: required by transaction-mode pooling.
            kwargs={"row_factory": dict_row, "connect_timeout": 10, "prepare_threshold": None},
        )

        self._opening: asyncio.Task[None] | None = None

    async def open(self) -> None:
        await self._pool.open()
        try:
            async with self._connection():
                pass
        except Exception:
            await self._pool.close()
            raise

    def open_in_background(self) -> None:
        """Start connecting now; cache hits at call start need not wait for Postgres."""
        if self._opening is None:
            self._opening = asyncio.ensure_future(self.open())

    async def ready(self) -> None:
        if self._opening is not None:
            await asyncio.shield(self._opening)

    async def close(self) -> None:
        if self._opening is not None and not self._opening.done():
            self._opening.cancel()
            with contextlib.suppress(BaseException):
                await self._opening
        elif self._opening is not None and not self._opening.cancelled():
            self._opening.exception()  # mark a failed background open as observed
        await self._pool.close()

    @asynccontextmanager
    async def connection(
        self, clinic_id: UUID | None = None
    ) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
        await self.ready()
        async with self._connection(clinic_id) as conn:
            yield conn

    @asynccontextmanager
    async def _connection(
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
