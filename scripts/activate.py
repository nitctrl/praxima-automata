"""Guarded development setup. No real phone changes, password output or fake Auth users."""

import argparse
import getpass
import sys
from pathlib import Path

import httpx
import psycopg
from dotenv import dotenv_values

from praxima.dev.activation import (
    bootstrap_owner,
    development_settings,
    provision_keys,
    publish_fictional,
    verify_history,
)
from praxima.shared.db.settings import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["keys", "register", "bootstrap", "publish"])
    parser.add_argument("--confirm-development-project", required=True)
    parser.add_argument("--email")
    args = parser.parse_args()
    try:
        config = development_settings(ROOT, args.confirm_development_project)
        with psycopg.connect(config.dsn, connect_timeout=10, autocommit=True) as conn:
            verify_history(conn, ROOT)
            if args.action == "keys":
                provision_keys(ROOT, config.project_ref)
                print("Private development keys ready; existing keys and real routing unchanged.")
            elif not args.email:
                raise ConfigurationError("Explicit owner email required")
            elif args.action == "register":
                if conn.execute(
                    "SELECT 1 FROM auth.users WHERE lower(email)=lower(%s)", (args.email,)
                ).fetchone():
                    print("Auth account already exists. Verify its email before bootstrap.")
                    return 0
                values = dotenv_values(ROOT / ".env")
                key = values.get("SUPABASE_PUBLISHABLE_KEY") or ""
                if not key.startswith("sb_publishable_"):
                    raise ConfigurationError("Publishable Auth key required")
                if not sys.stdin.isatty():
                    raise ConfigurationError("Password must be entered in a private terminal")
                password = getpass.getpass("Create a NEW development dashboard password (hidden): ")
                if len(password) < 12:
                    raise ConfigurationError("Use a new password of at least 12 characters")
                result = httpx.post(
                    f"https://{config.project_ref}.supabase.co/auth/v1/signup",
                    headers={"apikey": key},
                    json={"email": args.email, "password": password},
                    timeout=15,
                    follow_redirects=False,
                )
                del password
                if result.status_code >= 400:
                    print(
                        "Auth signup not completed. Use Supabase Auth console; details suppressed."
                    )
                    return 1
                print("Auth signup submitted. Confirm the email if requested, then run bootstrap.")
            elif args.action == "bootstrap":
                bootstrap_owner(conn, ROOT, args.email)
                print("Verified account assigned owner of fictional clinic A only.")
            else:
                version = publish_fictional(conn, ROOT, args.email)
                print(f"Fictional development publication ready: {version}. Real phone unchanged.")
        return 0
    except (ConfigurationError, ValueError, OSError):
        print(
            "Setup refused. Check project, verified account, fixture and private-file permissions."
        )
    except psycopg.Error as exc:
        print(
            f"Database setup failed (SQLSTATE {exc.sqlstate or 'connection'}); details suppressed."
        )
    except httpx.HTTPError:
        print("Auth service unavailable; no account creation confirmed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
