"""Register the domain packs shipped in src/praxima/packs so new workspaces can use them.

    uv run python scripts/packs.py register          # every shipped pack
    uv run python scripts/packs.py register clinic   # just one
    uv run python scripts/packs.py list              # what the database has

Uses DB_OWNER_DATABASE_URL (tenancy.pack_versions is an owner-managed platform table).
Idempotent. A released version is immutable: if a pack's files changed but its `version` in
manifest.yaml didn't, registration is refused; bump the version instead.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from praxima.packs import loader
from praxima.shared.db.engine import create_engine
from praxima.shared.db.settings import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]


def owner_engine() -> AsyncEngine:
    url = os.environ.get("DB_OWNER_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "DB_OWNER_DATABASE_URL"
    )
    if not url:
        sys.exit("DB_OWNER_DATABASE_URL is not set (in the environment or .env).")
    try:
        return create_engine(url, pooled=False)
    except ConfigurationError:
        sys.exit("DB_OWNER_DATABASE_URL must start with postgresql://")


async def register(keys: list[str]) -> int:
    engine = owner_engine()
    failed = 0
    try:
        async with engine.begin() as connection:
            for key in keys:
                try:
                    pack = loader.load(key)
                except loader.PackError as exc:
                    print(f"✗ {key}: invalid pack ({exc})")
                    failed += 1
                    continue
                existing = await connection.scalar(
                    text(
                        "SELECT checksum FROM tenancy.pack_versions "
                        "WHERE pack_key = :key AND version = :version"
                    ),
                    {"key": pack.key, "version": pack.version},
                )
                if existing == pack.checksum():
                    print(f"• {pack.key} {pack.version}: already registered")
                    continue
                if existing is not None:
                    print(
                        f"✗ {pack.key} {pack.version}: files changed but the version didn't; "
                        "bump `version` in manifest.yaml"
                    )
                    failed += 1
                    continue
                await connection.execute(
                    text(
                        "INSERT INTO tenancy.pack_versions (pack_key, version, manifest, checksum) "
                        "VALUES (:key, :version, CAST(:manifest AS jsonb), :checksum)"
                    ),
                    {
                        "key": pack.key,
                        "version": pack.version,
                        "manifest": json.dumps(pack.payload()),
                        "checksum": pack.checksum(),
                    },
                )
                print(f"✓ {pack.key} {pack.version}: registered ({pack.name}, {pack.industry})")
    finally:
        await engine.dispose()
    return 1 if failed else 0


async def show() -> int:
    engine = owner_engine()
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT pack_key, version, status, manifest->>'name' "
                        "FROM tenancy.pack_versions ORDER BY pack_key, version"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    if not rows:
        print("No packs registered. Run: uv run python scripts/packs.py register")
    for key, version, status, name in rows:
        print(f"{key:<16} {version:<10} {status:<11} {name}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("register", help="register shipped packs (idempotent)")
    add.add_argument(
        "packs", nargs="*", help=f"pack keys (default: {', '.join(loader.available())})"
    )
    commands.add_parser("list", help="show registered pack versions")
    args = parser.parse_args()
    try:
        if args.command == "register":
            sys.exit(asyncio.run(register(args.packs or loader.available())))
        sys.exit(asyncio.run(show()))
    except DBAPIError as exc:  # never print the URL; the first line says what went wrong
        detail = str(exc.orig).splitlines()[0] if exc.orig else type(exc).__name__
        sys.exit(
            f"✗ Database error: {detail}\n  Check it with: uv run python scripts/check_database.py"
        )


if __name__ == "__main__":
    main()
