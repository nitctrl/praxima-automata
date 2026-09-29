"""The HTTP API as an ASGI app: `uv run uvicorn main:app --reload --port 8080`.

Serves the legacy /api/* routes and the REST /api/v1; the Next.js frontend proxies /api/* here.
Settings come from the environment, with `.env` filling in only the names listed below.
"""

import os
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI

from praxima.dev.activation import DEV_FILE, load_development
from praxima.entrypoints.api import WebSettings, create_app
from praxima.integrations.llm.gemini import gemini_answerer
from praxima.modules.engagement import Vault
from praxima.modules.knowledge.infrastructure.qdrant_index import QdrantKnowledgeIndex
from praxima.shared.db.engine import bypasses_rls, create_engine, session_factory
from praxima.shared.db.settings import ConfigurationError

ROOT = Path(__file__).resolve().parent

# Only these enter server memory; none of them ever reaches the browser.
ENV_NAMES = (
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "DASHBOARD_ORIGIN",
    "QDRANT_URL",
    "QDRANT_COLLECTION",
    "QDRANT_EMBEDDING_MODEL",
    "QDRANT_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_MODEL",
    "APP_API_DATABASE_URL",
    "PRAXIMA_KNOWLEDGE_COLLECTION",
    "CLINIC_PII_KEYS",
    "CLINIC_PII_KEY_VERSION",
    "PRAXIMA_LOOKUP_KEY",
    "PRAXIMA_SELF_SIGNUP",
)


def build_app() -> FastAPI:
    values = dotenv_values(ROOT / ".env")
    for name in ENV_NAMES:
        if values.get(name):
            os.environ.setdefault(name, values[name] or "")
    try:
        settings = WebSettings.from_environment()
    except (KeyError, ValueError):
        raise SystemExit(
            "Dashboard requires valid Supabase URL, publishable key and origin."
        ) from None
    cipher = None
    if (ROOT / DEV_FILE).exists() or (ROOT / DEV_FILE).is_symlink():
        try:
            _, cipher = load_development(ROOT, values.get("SUPABASE_PROJECT_REF") or "")
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
        # A superuser or BYPASSRLS login (e.g. Supabase's `postgres`) would show every tenant's
        # data to everyone. Unreachable → start anyway; /api/v1 answers 503 until it's back.
        if bypasses_rls(os.environ["APP_API_DATABASE_URL"]):
            raise SystemExit(
                "APP_API_DATABASE_URL logs in as a role that bypasses row-level security, so "
                "every user would see every organization. Use a restricted login instead: "
                "uv run python scripts/api_role.py grant <role>"
            )
    # Semantic knowledge search when Qdrant + fastembed are available; else keywords only.
    knowledge_index = QdrantKnowledgeIndex.from_environment()
    # CRM personal data needs encryption and lookup keys; without them those endpoints 503.
    try:
        vault: Vault | None = Vault.from_environment()
    except ValueError:
        vault = None
    return create_app(
        settings,
        cipher=cipher,
        answerer=answerer,
        api_sessions=api_sessions,
        knowledge_index=knowledge_index,
        vault=vault,
        # Anyone may register and create their own organization (off unless "true").
        self_signup=os.environ.get("PRAXIMA_SELF_SIGNUP", "").strip().lower() == "true",
    )


app = build_app()
