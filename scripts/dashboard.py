"""Run one loopback dashboard API worker; the Next.js frontend proxies /api/* to it."""

import os
from pathlib import Path

import uvicorn
from dotenv import dotenv_values

from praxima.dev.activation import DEV_FILE, load_development
from praxima.entrypoints.api import WebSettings, create_app
from praxima.integrations.llm.gemini import gemini_answerer
from praxima.shared.db.engine import create_engine, session_factory
from praxima.shared.db.settings import ConfigurationError


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env")
    # Only the answer-generation provider key enters server memory; it never reaches the browser.
    for name in (
        "SUPABASE_URL",
        "SUPABASE_PUBLISHABLE_KEY",
        "CLINIC_DASHBOARD_ORIGIN",
        "QDRANT_URL",
        "QDRANT_COLLECTION",
        "QDRANT_EMBEDDING_MODEL",
        "QDRANT_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_MODEL",
        "APP_API_DATABASE_URL",
    ):
        if values.get(name):
            os.environ.setdefault(name, values[name] or "")
    try:
        settings = WebSettings.from_environment()
    except (KeyError, ValueError):
        raise SystemExit(
            "Dashboard requires valid Supabase URL, publishable key and origin."
        ) from None
    cipher = None
    if (root / DEV_FILE).exists() or (root / DEV_FILE).is_symlink():
        try:
            _, cipher = load_development(root, values.get("SUPABASE_PROJECT_REF") or "")
        except (ValueError, OSError):
            raise SystemExit(
                "Invalid private development settings; dashboard startup refused."
            ) from None
    answerer = None
    if values.get("GOOGLE_API_KEY"):
        answerer = gemini_answerer(
            values["GOOGLE_API_KEY"] or "",
            values.get("GEMINI_MODEL") or "gemini-2.5-flash",
        )
    api_sessions = None
    if os.environ.get("APP_API_DATABASE_URL"):
        try:
            api_sessions = session_factory(create_engine(os.environ["APP_API_DATABASE_URL"]))
        except ConfigurationError:
            raise SystemExit("APP_API_DATABASE_URL must be a postgresql:// URL.") from None
    uvicorn.run(
        create_app(settings, cipher=cipher, answerer=answerer, api_sessions=api_sessions),
        host="127.0.0.1",
        port=8080,
        access_log=False,
        proxy_headers=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
