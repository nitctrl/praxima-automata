"""The transactional outbox (ops.outbox, migration 0014): queue a job in the caller's
transaction, so it is sent if and only if the change commits. The background worker
(`entrypoints/jobs.py`) claims due jobs with `ops.claim_outbox` and runs each in its workspace.
Payloads hold ids only, never personal data.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_ATTEMPTS = 8

_ENQUEUE = text(
    "INSERT INTO ops.outbox (workspace_id, kind, payload)"
    " VALUES (:ws, :kind, CAST(:payload AS jsonb))"
)
_DONE = text("UPDATE ops.outbox SET status = 'done', updated_at = now() WHERE id = :id")
_RETRY = text(
    "UPDATE ops.outbox SET status = CASE WHEN attempts >= :max THEN 'failed' ELSE 'pending' END,"
    " next_attempt_at = now() + make_interval(secs => :delay), last_error_code = :code,"
    " updated_at = now() WHERE id = :id"
)


async def enqueue(
    session: AsyncSession, workspace_id: uuid.UUID, kind: str, payload: dict[str, Any]
) -> None:
    await session.execute(
        _ENQUEUE, {"ws": workspace_id, "kind": kind, "payload": json.dumps(payload, default=str)}
    )


async def mark_done(session: AsyncSession, job_id: uuid.UUID) -> None:
    await session.execute(_DONE, {"id": job_id})


async def mark_retry(session: AsyncSession, job_id: uuid.UUID, attempts: int, code: str) -> None:
    """Back off 30 s, 1 min, 2 min … (capped at an hour); give up after MAX_ATTEMPTS."""
    delay = min(30 * 2 ** max(attempts - 1, 0), 3600)
    await session.execute(
        _RETRY, {"id": job_id, "max": MAX_ATTEMPTS, "delay": delay, "code": code[:60]}
    )


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    workspace_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int


async def claim(session: AsyncSession, limit: int) -> list[Job]:
    """Lease due jobs across workspaces (ops.claim_outbox, SECURITY DEFINER)."""
    rows = await session.execute(
        text("SELECT id, workspace_id, kind, payload, attempts FROM ops.claim_outbox(:n)"),
        {"n": limit},
    )
    return [Job(*row) for row in rows.tuples()]


async def give_up(session: AsyncSession, job_id: uuid.UUID, code: str) -> None:
    """Stop retrying (e.g. access revoked): staff must act first."""
    await session.execute(
        text(
            "UPDATE ops.outbox SET status = 'failed', last_error_code = :code,"
            " updated_at = now() WHERE id = :id"
        ),
        {"id": job_id, "code": code[:60]},
    )
