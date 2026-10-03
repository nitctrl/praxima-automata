"""Check the /api/v1 database connection: `uv run python scripts/check_database.py`.

Connects with APP_API_DATABASE_URL (from the environment or .env) exactly as the API does and
prints what's wrong. It never prints the password or the full URL.
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url

from praxima.shared.db.engine import create_engine
from praxima.shared.db.settings import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]

HINTS = {
    "password authentication failed": "Wrong user or password. Special characters in the "
    "password must be URL-encoded (e.g. @ → %40, # → %23, / → %2F).",
    "does not exist": "The database name at the end of the URL doesn't exist (createdb it, "
    "or fix the name).",
    "Connection refused": "Nothing is listening there. Start Postgres "
    "(sudo systemctl start postgresql) or fix the host/port.",
    "Network is unreachable": "Supabase's direct host (db.<ref>.supabase.co) is IPv6-only. "
    "Use the session pooler URL (…pooler.supabase.com, port 5432) instead.",
    "could not translate host name": "The host name doesn't resolve. Check the host part.",
    "timeout": "No answer in time: wrong host/port, a firewall, or the transaction pooler "
    "(6543). Use port 5432.",
    "SSL": "SSL problem: Supabase needs ?sslmode=require; a local Postgres usually doesn't.",
}


async def check(url: str) -> int:
    parts = make_url(url)
    print(
        f"Connecting as {parts.username!r} to {parts.host or 'local socket'}:{parts.port or 5432}"
        f" database {parts.database!r} …"
    )
    try:
        engine = create_engine(url, pooled=False)
    except ConfigurationError:
        print("✗ APP_API_DATABASE_URL must start with postgresql://")
        return 1
    try:
        async with engine.connect() as connection:
            user = await connection.scalar(text("SELECT current_user"))
            ready = await connection.scalar(text("SELECT to_regclass('iam.users') IS NOT NULL"))
            powerful = await connection.scalar(
                text(
                    "SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles "
                    "WHERE rolname = current_user"
                )
            )
            revision = (
                await connection.scalar(text("SELECT version_num FROM ops.alembic_version"))
                # The restricted API login may not read Alembic's table (owner only): fine.
                if await connection.scalar(
                    text(
                        "SELECT to_regclass('ops.alembic_version') IS NOT NULL AND "
                        "has_table_privilege('ops.alembic_version', 'SELECT')"
                    )
                )
                else None
            )
    except Exception as exc:  # report any failure, never the URL
        message = str(getattr(exc, "orig", exc)).splitlines()[0] if str(exc) else ""
        print(f"✗ {type(getattr(exc, 'orig', exc)).__name__}: {message}")
        for needle, hint in HINTS.items():
            if needle.lower() in message.lower():
                print(f"  → {hint}")
                break
        return 1
    finally:
        await engine.dispose()
    print(f"✓ Connected as {user}.")
    if powerful:
        print(
            f"✗ {user} bypasses row-level security: every user would see every organization, "
            "and the API refuses to start. Use a restricted login: "
            "uv run python scripts/api_role.py grant <role>"
        )
        return 1
    if not ready:
        print("✗ The new schema is missing: run `uv run alembic upgrade head`.")
        return 1
    print(f"✓ New schema present (migration {revision or 'unknown'}).")
    return 0


def main() -> None:
    url = os.environ.get("APP_API_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "APP_API_DATABASE_URL"
    )
    if not url:
        sys.exit("✗ APP_API_DATABASE_URL is not set (in the environment or .env).")
    sys.exit(asyncio.run(check(url)))


if __name__ == "__main__":
    main()
