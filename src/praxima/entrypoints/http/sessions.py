"""Browser login sessions and login rate limiting, held in server memory.

The browser only gets an opaque HttpOnly cookie id and a CSRF token; no provider token is
kept after login. Single worker only: several workers need a shared store (e.g. Redis).
"""

import secrets
import time
import uuid
from dataclasses import dataclass, field

from praxima.shared.errors import RateLimited, Unavailable

COOKIE_NAME = "praxima_session"


@dataclass(frozen=True)
class WebSession:
    user_id: uuid.UUID
    email: str
    display_name: str | None
    csrf: str = field(repr=False)
    expires: float


class SessionStore:
    def __init__(self, ttl_seconds: int = 900, capacity: int = 1000) -> None:
        self.ttl = ttl_seconds
        self.capacity = capacity
        self._sessions: dict[str, WebSession] = {}

    def create(
        self, user_id: uuid.UUID, email: str, display_name: str | None
    ) -> tuple[str, WebSession]:
        self._purge()
        if len(self._sessions) >= self.capacity:
            raise Unavailable("Too many active sessions. Try again shortly.")
        sid = secrets.token_urlsafe(32)
        session = WebSession(
            user_id, email, display_name, secrets.token_urlsafe(32), time.monotonic() + self.ttl
        )
        self._sessions[sid] = session
        return sid, session

    def get(self, sid: str) -> WebSession | None:
        session = self._sessions.get(sid)
        if session is None:
            return None
        if session.expires <= time.monotonic():
            del self._sessions[sid]
            return None
        return session

    def delete(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    def clear(self) -> None:
        self._sessions.clear()

    def _purge(self) -> None:
        now = time.monotonic()
        for sid in [s for s, value in self._sessions.items() if value.expires <= now]:
            del self._sessions[sid]


class RateLimiter:
    """Fixed one-minute windows per key (e.g. login attempts per client address)."""

    def __init__(self, per_minute: int, max_keys: int = 5000) -> None:
        self.per_minute = per_minute
        self.max_keys = max_keys
        self._windows: dict[str, tuple[float, int]] = {}

    def hit(self, key: str) -> None:
        now = time.monotonic()
        if len(self._windows) >= self.max_keys:
            self._windows = {k: v for k, v in self._windows.items() if now - v[0] < 60}
            if len(self._windows) >= self.max_keys:
                raise RateLimited()
        start, count = self._windows.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        if count >= self.per_minute:
            raise RateLimited()
        self._windows[key] = (start, count + 1)
