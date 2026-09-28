"""Give the voice worker's database login exactly one right: looking up a call's live release.

    uv run python scripts/voice_runtime.py grant <role>   # allow <role> to run the lookup
    uv run python scripts/voice_runtime.py check <role>   # confirm it can do nothing else

Create the login yourself first (as a database admin), e.g. in psql:
    CREATE ROLE praxima_voice LOGIN PASSWORD '<a long random password>';
then put its URL in the worker's environment as PRAXIMA_RUNTIME_DATABASE_URL. Uses
DB_OWNER_DATABASE_URL (the migration owner, which owns the lookup function).
"""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from praxima.shared.db.engine import create_engine
from praxima.shared.db.settings import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = "releases.live_release_for_number(text)"
ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def owner_url() -> str:
    url = os.environ.get("DB_OWNER_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "DB_OWNER_DATABASE_URL"
    )
    if not url:
        sys.exit("DB_OWNER_DATABASE_URL is not set (in the environment or .env).")
    return url


async def run(command: str, role: str) -> int:
    try:
        engine = create_engine(owner_url(), pooled=False)
    except ConfigurationError:
        sys.exit("DB_OWNER_DATABASE_URL must start with postgresql://")
    try:
        async with engine.begin() as connection:
            if command == "grant":
                # The role name is validated above; identifiers can't be bound parameters.
                await connection.execute(text(f'GRANT USAGE ON SCHEMA releases TO "{role}"'))
                await connection.execute(text(f'GRANT EXECUTE ON FUNCTION {FUNCTION} TO "{role}"'))
                print(f"✓ {role} may look up live releases (and nothing else is granted).")
                return 0
            can_lookup = await connection.scalar(
                text("SELECT has_function_privilege(:role, :fn, 'EXECUTE')"),
                {"role": role, "fn": FUNCTION},
            )
            tables = (
                (
                    await connection.execute(
                        text(
                            "SELECT DISTINCT table_schema || '.' || table_name "
                            "FROM information_schema.role_table_grants "
                            "WHERE grantee = :role AND table_schema IN "
                            "('iam','tenancy','agents','releases','catalog','knowledge',"
                            "'engagement','billing','audit','ops') ORDER BY 1"
                        ),
                        {"role": role},
                    )
                )
                .scalars()
                .all()
            )
            powerful = await connection.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = :role"),
                {"role": role},
            )
    except DBAPIError as exc:
        detail = str(exc.orig).splitlines()[0] if exc.orig else type(exc).__name__
        sys.exit(f"✗ Database error: {detail}")
    finally:
        await engine.dispose()
    print(f"{'✓' if can_lookup else '✗'} can look up live releases")
    print(f"{'✗' if tables else '✓'} table access: {', '.join(tables) or 'none'}")
    print(f"{'✗' if powerful else '✓'} superuser / bypasses RLS: {'yes' if powerful else 'no'}")
    return 0 if can_lookup and not tables and not powerful else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["grant", "check"])
    parser.add_argument("role", help="the voice worker's database login, e.g. praxima_voice")
    args = parser.parse_args()
    if not ROLE.fullmatch(args.role):
        sys.exit("Role names are lowercase letters, digits and underscores.")
    sys.exit(asyncio.run(run(args.command, args.role)))


if __name__ == "__main__":
    main()
