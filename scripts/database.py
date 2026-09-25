"""Explicit development migrations, seed and runtime credential provisioning.

No implicit localhost target, destructive reset, raw exception or credential output.
"""

import argparse
import hashlib
import os
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import psycopg
from dotenv import dotenv_values
from psycopg import sql

from praxima.dev.development import seed_fictional_clinics
from praxima.shared.db.settings import ConfigurationError, DatabaseSettings

ROOT = Path(__file__).resolve().parents[1]


def settings(confirmed_project: str) -> DatabaseSettings:
    env = dotenv_values(ROOT / ".env")
    project = env.get("SUPABASE_PROJECT_REF", "") or ""
    if not confirmed_project or confirmed_project != project:
        raise ConfigurationError(
            "Explicit development project confirmation must match configuration."
        )
    return DatabaseSettings.validate(env.get("MIGRATION_DATABASE_URL", "") or "", project)


def migrate(conn: psycopg.Connection[Any]) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout = '60s'")
        conn.execute("SET LOCAL lock_timeout = '10s'")
        conn.execute("SELECT pg_advisory_xact_lock(1709202601)")
        conn.execute("CREATE SCHEMA IF NOT EXISTS clinic_migrations")
        conn.execute("REVOKE ALL ON SCHEMA clinic_migrations FROM PUBLIC, anon, authenticated")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS clinic_migrations.history ("
            "name text PRIMARY KEY, sha256 text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        recorded = dict(
            conn.execute("SELECT name, sha256 FROM clinic_migrations.history").fetchall()
        )
        files = sorted((ROOT / "supabase/migrations").glob("*.sql"))
        if set(recorded) - {path.name for path in files}:
            raise ConfigurationError(
                "Database contains an unknown migration; refusing to continue."
            )
        if not recorded:
            occupied = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE')"
            ).fetchone()
            if occupied and occupied[0]:
                raise ConfigurationError(
                    "Initial migration requires an empty dedicated public schema."
                )
        for path in files:
            text = path.read_text()
            checksum = hashlib.sha256(text.encode()).hexdigest()
            if path.name in recorded:
                if recorded[path.name] != checksum:
                    raise ConfigurationError(
                        "Applied migration checksum changed; use a new migration."
                    )
                continue
            if recorded and path.name < max(recorded):
                raise ConfigurationError("Out-of-order migration refused.")
            conn.execute(text, prepare=False)
            conn.execute(
                "INSERT INTO clinic_migrations.history (name,sha256) VALUES (%s,%s)",
                (path.name, checksum),
            )
            print(f"Applied: {path.name}")


def verify_migration_state(conn: psycopg.Connection[Any]) -> None:
    expected = {
        path.name: hashlib.sha256(path.read_text().encode()).hexdigest()
        for path in sorted((ROOT / "supabase/migrations").glob("*.sql"))
    }
    actual = dict(conn.execute("SELECT name, sha256 FROM clinic_migrations.history").fetchall())
    if not expected or actual != expected:
        raise ConfigurationError("Apply and verify all current migrations before this operation.")


def provision_runtime(conn: psycopg.Connection[Any], config: DatabaseSettings) -> None:
    """Create a private credential artifact; never overwrite .env or existing credentials."""
    row = conn.execute(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'clinic_runtime'"
    ).fetchone()
    if not row or row[0]:
        raise ConfigurationError("Initial provisioning requires an existing NOLOGIN runtime role.")
    destination = ROOT / ".env.runtime"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        password = secrets.token_urlsafe(48)
        uri = urlsplit(config.dsn)
        user = "clinic_runtime"
        if unquote(uri.username or "").endswith(f".{config.project_ref}"):
            user += f".{config.project_ref}"
        netloc = f"{user}:{quote(password, safe='')}@{uri.hostname}:{uri.port or 5432}"
        runtime_url = urlunsplit((uri.scheme, netloc, uri.path, uri.query, ""))
        with os.fdopen(descriptor, "w") as output:
            output.write(
                "# Generated development runtime secret. Do not commit.\n"
                f"DATABASE_URL={runtime_url}\n"
            )
            output.flush()
            os.fsync(output.fileno())
        with conn.transaction():
            conn.execute(
                sql.SQL("ALTER ROLE clinic_runtime LOGIN PASSWORD {}").format(sql.Literal(password))
            )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    print("Restricted runtime credential created in ignored .env.runtime (mode 0600).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["migrate", "seed", "provision-runtime", "status"])
    parser.add_argument("--confirm-development-project", required=True)
    args = parser.parse_args()
    try:
        config = settings(args.confirm_development_project)
        with psycopg.connect(config.dsn, connect_timeout=10, autocommit=True) as conn:
            if args.action == "migrate":
                migrate(conn)
            elif args.action == "seed":
                with conn.transaction():
                    conn.execute("SET LOCAL statement_timeout = '30s'")
                    conn.execute("SELECT pg_advisory_xact_lock(1709202601)")
                    verify_migration_state(conn)
                    seed_fictional_clinics(conn)
                print("Fictional fixtures ready; no live route assigned.")
            elif args.action == "provision-runtime":
                with conn.transaction():
                    conn.execute("SET LOCAL statement_timeout = '30s'")
                    conn.execute("SELECT pg_advisory_xact_lock(1709202601)")
                    verify_migration_state(conn)
                    provision_runtime(conn, config)
            else:
                rows = conn.execute(
                    "SELECT name FROM clinic_migrations.history ORDER BY name"
                ).fetchall()
                print(f"Applied migration count: {len(rows)}")
                for row in rows:
                    print(row[0])
        return 0
    except (ConfigurationError, OSError):
        print("Setup refused: check project confirmation, migration state and file permissions.")
    except psycopg.Error as exc:
        print(
            f"Database operation failed (SQLSTATE {exc.sqlstate or 'connection'}); "
            "details suppressed."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
