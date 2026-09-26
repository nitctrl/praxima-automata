"""Per-request context shared by logging and error handling (asyncio-safe)."""

from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
