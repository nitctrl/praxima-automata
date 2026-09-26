"""Alembic environment for the new schema on an external Postgres; never the legacy `public`."""

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import dotenv_values
from sqlalchemy import create_engine, pool, text

from praxima.shared.db.base import MODULE_SCHEMAS, PARTITION_NAME, Base
from praxima.shared.db.registry import import_models

ROOT = Path(__file__).resolve().parents[2]
VERSION_SCHEMA = "ops"
LOCK_KEY = 1709202602  # distinct from scripts/database.py's lock

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

import_models()
target_metadata = Base.metadata


def owner_url() -> str:
    """The owner (migration) role URL. Read at runtime; never printed or logged."""
    url = os.environ.get("DB_OWNER_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "DB_OWNER_DATABASE_URL"
    )
    if not url:
        raise SystemExit("DB_OWNER_DATABASE_URL is not set. Add it to .env (see .env.example).")
    # Accept the plain form that Supabase and psql tools print; use the psycopg 3 driver.
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url.removeprefix(prefix)
    if not url.startswith("postgresql+psycopg://"):
        raise SystemExit("DB_OWNER_DATABASE_URL must start with postgresql:// .")
    return url


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    # Autogenerate only ever looks at our module schemas, never Supabase's own schemas.
    return name in MODULE_SCHEMAS if type_ == "schema" else True


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    # Monthly partitions are created by SQL functions, not models: never diff or drop them.
    return not (type_ == "table" and reflected and name and PARTITION_NAME.fullmatch(name))


def configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
        version_table_schema=VERSION_SCHEMA,
        compare_type=True,
        **kwargs,  # type: ignore[arg-type]  # forwarded as-is to Alembic
    )


def run_offline() -> None:
    """Emit SQL without a database (`alembic upgrade head --sql`), e.g. for squawk in CI."""
    configure(dialect_name="postgresql", literal_binds=True)
    with context.begin_transaction():
        context.execute(f"CREATE SCHEMA IF NOT EXISTS {VERSION_SCHEMA}")
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(owner_url(), poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {VERSION_SCHEMA}"))
        connection.commit()
        configure(connection=connection)
        with context.begin_transaction():
            # One runner at a time; fail fast instead of queueing behind long locks.
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            connection.execute(text("SET LOCAL statement_timeout = '120s'"))
            # Extension types (citext) and operator classes (gist) resolve from `extensions`.
            connection.execute(text("SET LOCAL search_path TO public, extensions"))
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
