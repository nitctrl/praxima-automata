"""Explicit fictional-development activation; never assign or dispatch a real phone."""

import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from dotenv import dotenv_values

from clinic.development import fixture_id
from clinic.privacy import PiiCipher
from clinic.settings import ConfigurationError, DatabaseSettings
from clinic.snapshot import Snapshot

DEV_FILE = ".env.clinic-dev"


def development_settings(root: Path, confirmation: str) -> DatabaseSettings:
    values = dotenv_values(root / ".env")
    if not confirmation or confirmation != values.get("SUPABASE_PROJECT_REF"):
        raise ConfigurationError("Explicit development project confirmation required")
    return DatabaseSettings.validate(values.get("MIGRATION_DATABASE_URL") or "", confirmation)


def verify_history(conn: psycopg.Connection[Any], root: Path) -> None:
    expected = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "supabase/migrations").glob("*.sql")
    }
    actual = dict(conn.execute("SELECT name,sha256 FROM clinic_migrations.history").fetchall())
    if not expected or expected != actual:
        raise ConfigurationError("Migration state does not match this checkout")


def verified_user(conn: psycopg.Connection[Any], email: str) -> UUID:
    rows = conn.execute(
        "SELECT id FROM auth.users WHERE lower(email)=lower(%s) "
        "AND email_confirmed_at IS NOT NULL AND deleted_at IS NULL "
        "AND (banned_until IS NULL OR banned_until<=now())",
        (email,),
    ).fetchall()
    if len(rows) != 1:
        raise ConfigurationError("Create and verify the requested Auth account first")
    return UUID(str(rows[0][0]))


def require_fictional(conn: psycopg.Connection[Any], clinic: UUID) -> None:
    if clinic != fixture_id("A"):
        raise ConfigurationError("This helper only activates fictional clinic A")
    row = conn.execute(
        "SELECT slug FROM clinics WHERE id=%s FOR NO KEY UPDATE", (clinic,)
    ).fetchone()
    phones = conn.execute(
        "SELECT provider,trusted_trunk_id FROM phone_numbers WHERE clinic_id=%s", (clinic,)
    ).fetchall()
    if not row or not phones or any(p != ("test", "fixture-only-not-a-live-trunk") for p in phones):
        raise ConfigurationError("Fictional-only route verification failed")


def provision_keys(root: Path, project: str) -> None:
    path = root / DEV_FILE
    if path.exists() or path.is_symlink():
        load_development(root, project)
        return  # Never rotate or overwrite an existing key artifact.
    key = base64.b64encode(os.urandom(32)).decode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("# Private fictional-development settings; never commit.\n")
        output.write(f"SUPABASE_PROJECT_REF={project}\nCLINIC_ENVIRONMENT=development\n")
        output.write(f"CLINIC_TEST_CLINIC_ID={fixture_id('A')}\n")
        output.write("CLINIC_PII_KEY_VERSION=dev-v1\n")
        output.write("CLINIC_PII_KEYS='" + json.dumps({"dev-v1": key}) + "'\n")
        output.flush()
        os.fsync(output.fileno())


def load_development(root: Path, project: str) -> tuple[UUID, PiiCipher]:
    descriptor = os.open(root / DEV_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 16_384
        ):
            raise ConfigurationError("Private development settings require owner-only mode 0600")
        values = dotenv_values(stream=stream)
    if (
        values.get("SUPABASE_PROJECT_REF") != project
        or values.get("CLINIC_ENVIRONMENT") != "development"
        or values.get("CLINIC_TEST_CLINIC_ID") != str(fixture_id("A"))
    ):
        raise ConfigurationError("Development settings scope mismatch")
    try:
        keys = json.loads(values.get("CLINIC_PII_KEYS") or "")
        version = values.get("CLINIC_PII_KEY_VERSION") or ""
        if not isinstance(keys, dict) or not keys or version not in keys:
            raise ValueError
        decoded = {name: base64.b64decode(value, validate=True) for name, value in keys.items()}
        if any(
            not isinstance(name, str) or not 1 <= len(name) <= 80 or len(key) != 32
            for name, key in decoded.items()
        ):
            raise ValueError
        return fixture_id("A"), PiiCipher(decoded, version)
    except (ValueError, TypeError):
        raise ConfigurationError("Invalid private key configuration") from None


def bootstrap_owner(conn: psycopg.Connection[Any], root: Path, email: str) -> UUID:
    clinic = fixture_id("A")
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout='15s'")
        conn.execute("SET LOCAL lock_timeout='3s'")
        verify_history(conn, root)
        require_fictional(conn, clinic)
        user = verified_user(conn, email)
        current = conn.execute(
            "SELECT auth_user_id,role,status FROM clinic_users WHERE clinic_id=%s", (clinic,)
        ).fetchall()
        if any(row[0] != user for row in current):
            raise ConfigurationError("Existing clinic memberships require manual review")
        if current and current != [(user, "owner", "active")]:
            raise ConfigurationError("Existing membership differs; no automatic role takeover")
        if not current:
            membership = conn.execute(
                "INSERT INTO clinic_users(clinic_id,auth_user_id,role) "
                "VALUES(%s,%s,'owner') RETURNING id",
                (clinic, user),
            ).fetchone()
            assert membership
            conn.execute(
                "INSERT INTO audit_logs(clinic_id,actor_id,action,resource_type,"
                "resource_id,correlation_id) VALUES(%s,%s,'dev_owner_bootstrap',"
                "'clinic_users',%s,%s)",
                (clinic, user, membership[0], uuid4()),
            )
    return user


def publish_fictional(conn: psycopg.Connection[Any], root: Path, email: str) -> UUID:
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout='15s'")
        conn.execute("SET LOCAL lock_timeout='3s'")
        verify_history(conn, root)
        clinic = fixture_id("A")
        require_fictional(conn, clinic)
        user = verified_user(conn, email)
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(user),))
        conn.execute("SET LOCAL ROLE authenticated")
        current = conn.execute(
            "SELECT id,schema_version,snapshot FROM configuration_versions "
            "WHERE clinic_id=%s AND status='published'",
            (clinic,),
        ).fetchone()
        if not current:
            raise ConfigurationError("An authorized fictional publication is required")
        if current[1] == 2:
            Snapshot.model_validate(current[2])
            return UUID(str(current[0]))
        snapshot, digest = conn.execute(
            "SELECT * FROM public.clinic_preview(%s,NULL)", (clinic,)
        ).fetchone() or (None, None)
        Snapshot.model_validate(snapshot)
        result = conn.execute(
            "SELECT version_id FROM public.clinic_publish(%s,%s,%s,NULL)",
            (clinic, digest, current[0]),
        ).fetchone()
        if not result:
            raise ConfigurationError("Publication unavailable")
        return UUID(str(result[0]))
