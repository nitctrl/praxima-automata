"""Grant the first platform admin (then manage admins from the dashboard's Platform area).

    uv run python scripts/platform_admin.py grant you@example.com
    uv run python scripts/platform_admin.py revoke someone@example.com
    uv run python scripts/platform_admin.py list

The account must exist: sign up or sign in once first. Uses DB_OWNER_DATABASE_URL
(platform_admins is owner-managed; the API may only change it through admin-only functions).
Prints user ids, never emails of other people.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from praxima.shared.db.engine import create_engine
from praxima.shared.db.settings import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]


def owner_url() -> str:
    url = os.environ.get("DB_OWNER_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "DB_OWNER_DATABASE_URL"
    )
    if not url:
        sys.exit("DB_OWNER_DATABASE_URL is not set (in the environment or .env).")
    return url


async def run(command: str, email: str) -> int:
    try:
        engine = create_engine(owner_url(), pooled=False)
    except ConfigurationError:
        sys.exit("DB_OWNER_DATABASE_URL must start with postgresql://")
    try:
        async with engine.begin() as connection:
            if command == "list":
                rows = (
                    await connection.execute(
                        text("SELECT user_id, created_at FROM iam.platform_admins ORDER BY 2")
                    )
                ).all()
                for user_id, created_at in rows:
                    print(f"{user_id}  since {created_at:%Y-%m-%d}")
                if not rows:
                    print("No platform admins yet. Grant one: platform_admin.py grant <email>")
                return 0
            user_id = await connection.scalar(
                text("SELECT id FROM iam.users WHERE email = :email"),
                {"email": email.strip().lower()},
            )
            if user_id is None:
                print("✗ No account with that email. Sign up or sign in once first.")
                return 1
            if command == "grant":
                await connection.execute(
                    text(
                        "INSERT INTO iam.platform_admins (user_id) VALUES (:id) "
                        "ON CONFLICT (user_id) DO NOTHING"
                    ),
                    {"id": user_id},
                )
                print(f"✓ {user_id} is a platform admin. Sign in again to see the Platform area.")
                return 0
            removed = await connection.execute(
                text("DELETE FROM iam.platform_admins WHERE user_id = :id"), {"id": user_id}
            )
            print(f"✓ {user_id} removed." if removed.rowcount else "• Not a platform admin.")
            return 0
    except DBAPIError as exc:
        detail = str(exc.orig).splitlines()[0] if exc.orig else type(exc).__name__
        sys.exit(f"✗ Database error: {detail}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["grant", "revoke", "list"])
    parser.add_argument("email", nargs="?", default="", help="the account's email")
    args = parser.parse_args()
    if args.command != "list" and "@" not in args.email:
        parser.error("grant and revoke need the account's email")
    sys.exit(asyncio.run(run(args.command, args.email)))


if __name__ == "__main__":
    main()
