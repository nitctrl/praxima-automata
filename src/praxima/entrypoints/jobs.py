"""Background worker: runs outbox jobs (Google Calendar sync) until stopped.

    uv run python -m praxima.entrypoints.jobs

Each loop it queues a busy-time sync for every calendar not synced for 5 minutes, then claims
due jobs (`ops.claim_outbox`, the only cross-tenant step) and runs each one in a transaction
scoped to its workspace, so forced RLS applies to everything a job reads or writes. Failures
retry with backoff; personal data and tokens are never logged.

Database login: PRAXIMA_WORKER_DATABASE_URL, else APP_API_DATABASE_URL (development). It must
not bypass RLS, like the API's.
"""

import asyncio
import logging
import os
import signal
import sys
import uuid
from pathlib import Path

from dotenv import dotenv_values

from praxima.integrations.google.calendar import GoogleCalendar, GoogleConfig
from praxima.modules import scheduling
from praxima.modules.engagement import Vault
from praxima.shared.db import outbox
from praxima.shared.db.engine import (
    Scope,
    SessionFactory,
    bypasses_rls,
    create_engine,
    scoped_transaction,
    session_factory,
)

logger = logging.getLogger("praxima.jobs")
ROOT = Path(__file__).resolve().parents[3]
ENV_NAMES = (
    "PRAXIMA_WORKER_DATABASE_URL",
    "APP_API_DATABASE_URL",
    "CLINIC_PII_KEYS",
    "CLINIC_PII_KEY_VERSION",
    "PRAXIMA_LOOKUP_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REDIRECT_URI",
)
BATCH = 20
IDLE_SECONDS = 5.0
BUSY_SYNC_MINUTES = 5


async def run_once(sessions: SessionFactory, vault: Vault, google: GoogleCalendar | None) -> int:
    """Queue due busy syncs, then claim and run one batch. Returns how many jobs ran."""
    async with scoped_transaction(sessions, Scope()) as session:
        await scheduling.queue_busy_syncs(session, BUSY_SYNC_MINUTES)
        jobs = await outbox.claim(session, BATCH)
    for job in jobs:
        await _run(sessions, vault, google, job)
    return len(jobs)


async def _run(
    sessions: SessionFactory, vault: Vault, google: GoogleCalendar | None, job: outbox.Job
) -> None:
    scope = Scope(workspace_id=job.workspace_id)
    try:
        async with scoped_transaction(sessions, scope) as session:
            await scheduling.run_job(session, vault, google, kind=job.kind, payload=job.payload)
            await outbox.mark_done(session, job.id)
        return
    except scheduling.JobFailed as exc:
        code, retryable = exc.code, exc.retryable
    except Exception as exc:  # unexpected: retry with backoff, log only the type
        code, retryable = type(exc).__name__, True
    logger.warning("Job %s (%s) failed: %s", job.id, job.kind, code)
    async with scoped_transaction(sessions, scope) as session:
        await scheduling.note_job_failure(
            session, kind=job.kind, payload=job.payload, code=code, retryable=retryable
        )
        if retryable:
            await outbox.mark_retry(session, job.id, job.attempts, code)
        else:  # e.g. access revoked: the job flagged the connection; staff reconnect first
            await outbox.give_up(session, job.id, code)


def _database_url() -> str:
    url = os.environ.get("PRAXIMA_WORKER_DATABASE_URL") or os.environ.get("APP_API_DATABASE_URL")
    if not url:
        sys.exit("Set PRAXIMA_WORKER_DATABASE_URL (or APP_API_DATABASE_URL in development).")
    if bypasses_rls(url):
        sys.exit("The worker's database login bypasses row-level security. Use a restricted one.")
    return url


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    values = dotenv_values(ROOT / ".env")
    for name in ENV_NAMES:
        if values.get(name):
            os.environ.setdefault(name, values[name] or "")
    try:
        vault = Vault.from_environment()
    except ValueError:
        sys.exit("CLINIC_PII_KEYS / PRAXIMA_LOOKUP_KEY are needed to read booking details.")
    config = GoogleConfig.from_environment()
    google = GoogleCalendar(config) if config else None
    if google is None:
        logger.warning("Google Calendar isn't configured: calendar jobs will fail and stop.")
    engine = create_engine(_database_url())
    sessions = session_factory(engine)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("Worker started (id %s)", uuid.uuid4().hex[:8])
    try:
        while not stop.is_set():
            try:
                ran = await run_once(sessions, vault, google)
            except Exception as exc:  # database down: wait and try again
                logger.warning("Worker loop failed: %s", type(exc).__name__)
                ran = 0
            if not ran:
                try:
                    await asyncio.wait_for(stop.wait(), IDLE_SECONDS)
                except TimeoutError:
                    pass
    finally:
        await engine.dispose()
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
