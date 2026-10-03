"""Google Calendar over HTTPS (OAuth 2.0 web flow): connect, events, free/busy, revoke.

Every call has a timeout. Tokens, bodies and URLs are never logged; failures surface as
`GoogleError(code)` with a short, fixed code the caller stores or retries on.
"""

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class GoogleError(Exception):
    """A Google call failed. `code`: revoked, not_found, rate_limited, rejected, unavailable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code in ("rate_limited", "unavailable")


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str
    client_secret: str
    redirect_uri: (
        str  # registered in the Google Cloud console; ends in /integrations/google/callback
    )

    @classmethod
    def from_environment(cls) -> "GoogleConfig | None":
        values = (
            os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
            os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
            os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip(),
        )
        if not all(values):
            return None
        if not values[2].startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError(
                "GOOGLE_OAUTH_REDIRECT_URI must be HTTPS (or loopback in development)."
            )
        return cls(*values)


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None


class GoogleCalendar:
    def __init__(self, config: GoogleConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport)

    def authorize_url(self, state: str) -> str:
        query = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",  # always return a refresh token
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise GoogleError("unavailable") from None
        if response.status_code < 400:
            return response
        if response.status_code == 400 and "invalid_grant" in response.text:
            raise GoogleError("revoked")
        if response.status_code in (401, 403):
            raise GoogleError("revoked" if response.status_code == 401 else "rejected")
        if response.status_code in (404, 410):
            raise GoogleError("not_found")
        if response.status_code == 429:
            raise GoogleError("rate_limited")
        if response.status_code >= 500:
            raise GoogleError("unavailable")
        raise GoogleError("rejected")

    async def exchange_code(self, code: str) -> Tokens:
        response = await self._request(
            "POST",
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        body = response.json()
        return Tokens(str(body["access_token"]), body.get("refresh_token"))

    async def access_token(self, refresh_token: str) -> str:
        response = await self._request(
            "POST",
            TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
            },
        )
        return str(response.json()["access_token"])

    async def account_email(self, access_token: str) -> str | None:
        response = await self._request(
            "GET", USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        email = response.json().get("email")
        return str(email) if email else None

    async def upsert_event(
        self, access_token: str, calendar_id: str, event_id: str | None, event: dict[str, Any]
    ) -> str:
        """Create the event, or update it when `event_id` is known. Returns its id."""
        headers = {"Authorization": f"Bearer {access_token}"}
        base = f"{API}/calendars/{quote(calendar_id, safe='')}/events"
        if event_id:
            try:
                response = await self._request(
                    "PATCH", f"{base}/{event_id}", headers=headers, json=event
                )
                return str(response.json()["id"])
            except GoogleError as exc:
                if exc.code != "not_found":
                    raise  # deleted in Google: create it again below
        response = await self._request("POST", base, headers=headers, json=event)
        return str(response.json()["id"])

    async def delete_event(self, access_token: str, calendar_id: str, event_id: str) -> None:
        base = f"{API}/calendars/{quote(calendar_id, safe='')}/events"
        try:
            await self._request(
                "DELETE", f"{base}/{event_id}", headers={"Authorization": f"Bearer {access_token}"}
            )
        except GoogleError as exc:
            if exc.code != "not_found":  # already gone
                raise

    async def busy(
        self, access_token: str, calendar_id: str, starts: datetime, ends: datetime
    ) -> list[tuple[datetime, datetime]]:
        response = await self._request(
            "POST",
            f"{API}/freeBusy",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "timeMin": starts.isoformat(),
                "timeMax": ends.isoformat(),
                "items": [{"id": calendar_id}],
            },
        )
        calendar = response.json().get("calendars", {}).get(calendar_id, {})
        if calendar.get("errors"):
            raise GoogleError("rejected")
        return [
            (
                datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            )
            for b in calendar.get("busy", [])
        ]

    async def revoke(self, token: str) -> None:
        """Best effort: the connection is removed on our side either way."""
        try:
            await self._request("POST", REVOKE_URL, data={"token": token})
        except GoogleError:
            pass


# ------------------------------------------------------------------ OAuth state
# The callback arrives as a top-level redirect from Google, so the SameSite=Strict session
# cookie isn't sent. The state carries who started the connection, signed and short-lived.

STATE_SECONDS = 600


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), b"praxima-calendar-state:" + body, hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")


def sign_state(secret: str, values: dict[str, str], now: float) -> str:
    body = json.dumps(values | {"exp": int(now) + STATE_SECONDS}, sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{encoded}.{_sign(secret, body)}"


def read_state(secret: str, state: str, now: float) -> dict[str, str] | None:
    """The signed values, or None if tampered with, malformed or expired."""
    try:
        encoded, signature = state.split(".", 1)
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _sign(secret, body)):
            return None
        values = json.loads(body)
        if not isinstance(values, dict) or int(values.pop("exp")) < now:
            return None
        return {str(k): str(v) for k, v in values.items()}
    except (ValueError, KeyError, TypeError):
        return None
