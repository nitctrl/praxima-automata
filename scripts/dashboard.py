"""Run one loopback dashboard worker; production requires an HTTPS reverse proxy."""

import os
from pathlib import Path

import uvicorn
from dotenv import dotenv_values

from clinic.activation import DEV_FILE, load_development
from clinic.dashboard import WebSettings, create_app


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env")
    # Deliberately do not import migration/runtime/provider secrets into this server.
    for name in (
        "SUPABASE_URL",
        "SUPABASE_PUBLISHABLE_KEY",
        "CLINIC_DASHBOARD_ORIGIN",
        "QDRANT_URL",
        "QDRANT_COLLECTION",
        "QDRANT_EMBEDDING_MODEL",
        "QDRANT_API_KEY",
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
    uvicorn.run(
        create_app(settings, cipher=cipher),
        host="127.0.0.1",
        port=8080,
        access_log=False,
        proxy_headers=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
