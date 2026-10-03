"""The Google Calendar client and the signed OAuth state (no network: a fake Google)."""

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from praxima.integrations.google.calendar import (
    GoogleCalendar,
    GoogleConfig,
    GoogleError,
    read_state,
    sign_state,
)

CONFIG = GoogleConfig(
    "client-1", "secret-1", "http://127.0.0.1:3000/api/v1/integrations/google/callback"
)


def client(handler) -> GoogleCalendar:  # type: ignore[no-untyped-def]
    return GoogleCalendar(CONFIG, httpx.MockTransport(handler))


def test_consent_url_asks_for_offline_calendar_access():
    query = parse_qs(urlsplit(client(lambda r: httpx.Response(200)).authorize_url("st")).query)
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["state"] == ["st"] and query["redirect_uri"] == [CONFIG.redirect_uri]
    scopes = query["scope"][0].split()
    assert "https://www.googleapis.com/auth/calendar.events" in scopes
    assert "https://www.googleapis.com/auth/calendar.freebusy" in scopes


def test_tokens_events_busy_and_errors():
    seen: list[tuple[str, str]] = []

    def google(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode()))
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1"})
            if form["refresh_token"] == ["gone"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "a2"})
        if request.url.path.endswith("/events") and request.method == "POST":
            return httpx.Response(200, json={"id": "new-event"})
        if request.url.path.endswith("/events/missing"):
            return httpx.Response(404)
        if request.url.path.endswith("/events/e1"):
            return httpx.Response(200 if request.method == "PATCH" else 204, json={"id": "e1"})
        if request.url.path.endswith("/freeBusy"):
            body = json.loads(request.content)
            assert body["items"] == [{"id": "dr@example.com"}]
            busy = [{"start": "2026-10-05T04:00:00Z", "end": "2026-10-05T04:30:00Z"}]
            return httpx.Response(200, json={"calendars": {"dr@example.com": {"busy": busy}}})
        if request.url.path.endswith("/limited"):
            return httpx.Response(429)
        return httpx.Response(500)

    calendar = client(google)

    async def run() -> None:
        tokens = await calendar.exchange_code("code")
        assert (tokens.access_token, tokens.refresh_token) == ("a1", "r1")
        assert await calendar.access_token("r1") == "a2"
        with pytest.raises(GoogleError) as revoked:
            await calendar.access_token("gone")
        assert revoked.value.code == "revoked" and not revoked.value.retryable
        assert await calendar.upsert_event("a", "dr@example.com", None, {}) == "new-event"
        assert await calendar.upsert_event("a", "dr@example.com", "e1", {}) == "e1"
        # Deleted in Google meanwhile: created again instead of failing.
        assert await calendar.upsert_event("a", "dr@example.com", "missing", {}) == "new-event"
        await calendar.delete_event("a", "dr@example.com", "missing")  # already gone: fine
        busy = await calendar.busy(
            "a",
            "dr@example.com",
            datetime(2026, 10, 5, tzinfo=timezone.utc),
            datetime(2026, 10, 6, tzinfo=timezone.utc),
        )
        assert busy == [
            (
                datetime(2026, 10, 5, 4, tzinfo=timezone.utc),
                datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc),
            )
        ]
        with pytest.raises(GoogleError) as unavailable:
            await calendar.delete_event("a", "x", "boom")
        assert unavailable.value.retryable

    asyncio.run(run())
    # Calendar ids (often email addresses) are quoted into the path.
    assert ("POST", "/calendar/v3/calendars/dr%40example.com/events") in seen


def test_state_is_signed_and_expires():
    state = sign_state("secret-1", {"ws": "w", "entity": "e", "user": "u"}, now=1000)
    assert read_state("secret-1", state, now=1500) == {"ws": "w", "entity": "e", "user": "u"}
    assert read_state("secret-1", state, now=1000 + 601) is None  # expired
    assert read_state("other-secret", state, now=1500) is None
    body, signature = state.split(".")
    assert read_state("secret-1", f"{body}x.{signature}", now=1500) is None  # tampered
    assert read_state("secret-1", "garbage", now=1500) is None


def test_config_needs_all_three_settings(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    assert GoogleConfig.from_environment() is None
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "http://example.com/callback")
    with pytest.raises(ValueError, match="HTTPS"):
        GoogleConfig.from_environment()
