"""Load the live release for one call: called number → agent → published snapshot.

One read-only call to `releases.live_release_for_number` (SECURITY DEFINER; the database
resolves the tenant from the trusted called number). No tables are read directly, and no
platform code is loaded. Any failure yields a reason, never a partial or guessed release.
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from pydantic import ValidationError

from praxima.modules.releases.domain.agent_snapshot import SCHEMA_VERSION, AgentSnapshot

logger = logging.getLogger(__name__)
LOAD_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class LoadedRelease:
    release_id: uuid.UUID
    version_no: int
    workspace_id: uuid.UUID
    agent_id: uuid.UUID
    snapshot: AgentSnapshot


@dataclass(frozen=True)
class NoRelease:
    """Why nothing was loaded: invalid_number, unknown_number, untrusted_trunk, agent_disabled,
    organization_inactive, no_live_release, runtime_database_not_configured,
    database_unavailable, unsupported_schema_version, invalid_snapshot."""

    reason: str


def runtime_database_url() -> str | None:
    """The restricted runtime login; falls back to the API login for local development."""
    url = os.environ.get("PRAXIMA_RUNTIME_DATABASE_URL") or ""
    if not url:
        url = os.environ.get("APP_API_DATABASE_URL") or ""
        if url:
            logger.warning(
                "PRAXIMA_RUNTIME_DATABASE_URL is not set; using the API database login. "
                "Use a restricted runtime login in production (scripts/voice_runtime.py)."
            )
    if not url:
        return None
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql://" + url.removeprefix(prefix)
    return url


def interpret(result: Any) -> LoadedRelease | NoRelease:
    """Validate what the lookup function returned. Refuses unknown snapshot versions."""
    if not isinstance(result, dict):
        return NoRelease("database_unavailable")
    if "reason" in result:
        return NoRelease(str(result["reason"]))
    snapshot = result.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        return NoRelease("unsupported_schema_version")
    try:
        return LoadedRelease(
            release_id=uuid.UUID(str(result["release_id"])),
            version_no=int(result["version_no"]),
            workspace_id=uuid.UUID(str(result["workspace_id"])),
            agent_id=uuid.UUID(str(result["agent_id"])),
            snapshot=AgentSnapshot.model_validate(snapshot),
        )
    except (KeyError, ValueError, TypeError, ValidationError):
        return NoRelease("invalid_snapshot")


async def load_release(
    called_number: str, trunk_id: str | None = None
) -> LoadedRelease | NoRelease:
    """SIP calls pass the trunk LiveKit reported (checked against the number's trusted
    trunk, migration 0015); console tests pass None."""
    url = runtime_database_url()
    if url is None:
        return NoRelease("runtime_database_not_configured")

    async def query() -> Any:
        async with await psycopg.AsyncConnection.connect(
            url, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as conn:
            await conn.execute("SET TRANSACTION READ ONLY")
            if trunk_id is None:
                lookup = conn.execute(
                    "SELECT releases.live_release_for_number(%s)", (called_number,)
                )
            else:
                lookup = conn.execute(
                    "SELECT releases.live_release_for_call(%s,%s)", (called_number, trunk_id)
                )
            row = await (await lookup).fetchone()
            await conn.rollback()  # read-only: nothing to keep, settings end with the transaction
            return row[0] if row else None

    try:
        result = await asyncio.wait_for(query(), LOAD_TIMEOUT_SECONDS)
    except Exception as exc:  # never log the URL or the number
        logger.warning("Release lookup failed (%s)", type(exc).__name__)
        return NoRelease("database_unavailable")
    return interpret(result)
