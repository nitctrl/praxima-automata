"""Give the API's database login table access under row-level security, and nothing more.

The API must never log in as the owner when the owner bypasses RLS (Supabase's `postgres`
does, as does any superuser): every signed-in user would then see every organization.

    uv run python scripts/api_role.py grant <role>   # module tables + platform admin functions
    uv run python scripts/api_role.py check <role>   # confirm it's restricted and complete

Create the login yourself first (as a database admin), e.g. in psql or Supabase's SQL editor:
    CREATE ROLE praxima_api LOGIN PASSWORD '<a long random password>';
then set APP_API_DATABASE_URL to connect as it (on Supabase's session pooler the user name is
`praxima_api.<project-ref>`). Re-run `grant` after every `alembic upgrade` that adds tables
or functions. Uses DB_OWNER_DATABASE_URL (the migration owner, which owns the tables).
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
SCHEMAS = (
    "iam",
    "tenancy",
    "agents",
    "releases",
    "catalog",
    "knowledge",
    "engagement",
    "scheduling",
    "audit",
)
# Platform tables: the API reads them; only migrations and scripts change them.
READ_ONLY = ("tenancy.pack_versions", "iam.platform_admins")
# Platform admin writes (migration 0010): each checks for a platform admin in the database.
FUNCTIONS = (
    "tenancy.admin_register_pack_version(text, text, jsonb, text)",
    "tenancy.admin_set_pack_status(text, text, text)",
    "iam.admin_list_platform_admins()",
    "iam.admin_grant_platform_admin(uuid)",
    "iam.admin_revoke_platform_admin(uuid)",
)
ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLES = text(
    "SELECT n.nspname || '.' || c.relname FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
    "ORDER BY 1"
)


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
            if not await connection.scalar(
                text("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :role"), {"role": role}
            ):
                sys.exit(f"✗ No role {role!r}. Create it first (see --help).")
            tables = (await connection.execute(TABLES, {"schemas": list(SCHEMAS)})).scalars().all()
            functions = [
                f
                for f in FUNCTIONS
                if await connection.scalar(text("SELECT to_regprocedure(:f)"), {"f": f})
            ]
            if command == "grant":
                # The role name is validated above; identifiers can't be bound parameters.
                for schema in SCHEMAS:
                    await connection.execute(text(f'GRANT USAGE ON SCHEMA {schema} TO "{role}"'))
                for table in tables:
                    rights = "SELECT" if table in READ_ONLY else "SELECT, INSERT, UPDATE, DELETE"
                    await connection.execute(text(f'GRANT {rights} ON {table} TO "{role}"'))
                for function in functions:
                    await connection.execute(
                        text(f'GRANT EXECUTE ON FUNCTION {function} TO "{role}"')
                    )
                print(f"✓ {role} may use {len(tables)} tables, under row-level security.")
                if len(functions) < len(FUNCTIONS):
                    print("  Platform admin functions are missing: run `alembic upgrade head`.")
                return 0
            missing = [
                table
                for table in tables
                if not await connection.scalar(
                    text("SELECT has_table_privilege(:role, :table, :rights)"),
                    {
                        "role": role,
                        "table": table,
                        "rights": "SELECT"
                        if table in READ_ONLY
                        else "SELECT, INSERT, UPDATE, DELETE",
                    },
                )
            ]
            ungranted = [
                f.split("(")[0]
                for f in FUNCTIONS
                if f not in functions
                or not await connection.scalar(
                    text("SELECT has_function_privilege(:role, :f, 'EXECUTE')"),
                    {"role": role, "f": f},
                )
            ]
            writable = [
                table
                for table in READ_ONLY
                if await connection.scalar(
                    text("SELECT has_table_privilege(:role, :table, 'INSERT, UPDATE, DELETE')"),
                    {"role": role, "table": table},
                )
            ]
            powerful = await connection.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = :r"),
                {"r": role},
            )
    except DBAPIError as exc:
        detail = str(exc.orig).splitlines()[0] if exc.orig else type(exc).__name__
        sys.exit(f"✗ Database error: {detail}")
    finally:
        await engine.dispose()
    print(
        f"{'✗' if missing else '✓'} table access"
        + (f" missing on: {', '.join(missing)} (run grant)" if missing else "")
    )
    print(
        f"{'✗' if writable else '✓'} platform tables read-only"
        + (f" (writable: {', '.join(writable)})" if writable else "")
    )
    print(
        f"{'✗' if ungranted else '✓'} platform admin functions"
        + (f" missing: {', '.join(ungranted)} (migrate, then grant)" if ungranted else "")
    )
    print(f"{'✗' if powerful else '✓'} superuser / bypasses RLS: {'yes' if powerful else 'no'}")
    return 0 if not missing and not writable and not ungranted and not powerful else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["grant", "check"])
    parser.add_argument("role", help="the API's database login, e.g. praxima_api")
    args = parser.parse_args()
    if not ROLE.fullmatch(args.role):
        sys.exit("Role names are lowercase letters, digits and underscores.")
    sys.exit(asyncio.run(run(args.command, args.role)))


if __name__ == "__main__":
    main()
