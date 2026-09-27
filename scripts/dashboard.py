"""Run the API as before (`uv run python scripts/dashboard.py`); the app itself is in main.py.

Equivalent to `uv run uvicorn main:app --port 8080 --no-access-log --no-proxy-headers`.
"""

from pathlib import Path

import uvicorn


def main() -> None:
    uvicorn.run(
        "main:app",
        app_dir=str(Path(__file__).resolve().parents[1]),
        host="127.0.0.1",
        port=8080,
        access_log=False,
        proxy_headers=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
