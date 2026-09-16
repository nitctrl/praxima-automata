"""
WebSocket server exposing the bridge to telephony carriers.
===========================================================

Routes
    GET  /health              liveness probe
    WS   /{provider}          carrier media stream (e.g. /exotel)

Run:
    uv run python -m bridge.server
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
import sys
from pathlib import Path

from aiohttp import WSMsgType, web
from dotenv import load_dotenv

# Allow `python -m bridge.server` from the repo root as well as `src/`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.config import BridgeConfig, ConfigError  # noqa: E402
from bridge.providers import PROVIDERS  # noqa: E402
from bridge.room import CallBridge  # noqa: E402

logger = logging.getLogger("bridge.server")

_CONFIG_KEY = web.AppKey("config", BridgeConfig)


class _AiohttpSocket:
    """Adapts `web.WebSocketResponse` to the bridge's `CallSocket` protocol."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self._ws = ws

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, str):
            await self._ws.send_str(message)
        else:
            await self._ws.send_bytes(message)

    async def __aiter__(self):
        async for msg in self._ws:
            if msg.type is WSMsgType.TEXT:
                yield msg.data
            elif msg.type is WSMsgType.BINARY:
                yield msg.data
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break
            elif msg.type is WSMsgType.ERROR:
                logger.warning("websocket error: %s", self._ws.exception())
                break


def _authorized(request: web.Request, config: BridgeConfig) -> bool:
    if not config.auth_required:
        return True

    # Carriers vary in what they support. Exotel's Voicebot applet takes a
    # single URL field, and not every carrier honours `wss://user:pass@host`
    # or passes query strings through intact, so accept the shared secret
    # either as `?token=` or as a trailing path segment.
    for candidate in (request.query.get("token", ""), request.match_info.get("token", "")):
        if candidate and secrets.compare_digest(candidate, config.auth_password):
            return True

    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False

    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False

    user, _, password = decoded.partition(":")
    # compare_digest on both fields to avoid leaking validity via timing.
    user_ok = secrets.compare_digest(user, config.auth_user)
    password_ok = secrets.compare_digest(password, config.auth_password)
    return user_ok and password_ok


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "providers": sorted(PROVIDERS)})


async def media(request: web.Request) -> web.StreamResponse:
    config = request.app[_CONFIG_KEY]

    # Authenticate before touching the registry, otherwise the 404-vs-401
    # difference lets an anonymous caller enumerate configured carriers.
    if not _authorized(request, config):
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="bridge"'})

    name = request.match_info["provider"].lower()
    factory = PROVIDERS.get(name)
    if factory is None:
        raise web.HTTPNotFound(text=f"unknown provider: {name}")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    provider = factory()

    # Carriers may negotiate the rate on the query string; the `start` event
    # still wins if it declares one.
    if raw_rate := request.query.get("sample-rate"):
        try:
            provider.default_sample_rate = int(raw_rate)
        except ValueError:
            logger.warning("ignoring bad sample-rate=%r", raw_rate)

    logger.info("call connected provider=%s", name)
    bridge = CallBridge(provider, config)
    try:
        await bridge.run(_AiohttpSocket(ws))
    finally:
        await bridge.aclose()
        if not ws.closed:
            await ws.close()
        logger.info("call disconnected provider=%s", name)

    return ws


def create_app(config: BridgeConfig) -> web.Application:
    app = web.Application()
    app[_CONFIG_KEY] = config
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/{provider}", media),
            web.get("/{provider}/{token}", media),
        ]
    )
    return app


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("BRIDGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )

    try:
        config = BridgeConfig.from_env()
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc

    if not config.auth_required:
        logger.warning(
            "BRIDGE_AUTH_USER/BRIDGE_AUTH_PASSWORD unset - the media endpoint "
            "is UNAUTHENTICATED. Set them before exposing this publicly."
        )

    logger.info(
        "bridge listening on %s:%d providers=%s agent=%s",
        config.host,
        config.port,
        sorted(PROVIDERS),
        config.agent_name,
    )
    web.run_app(
        create_app(config),
        host=config.host,
        port=config.port,
        print=None,
    )


if __name__ == "__main__":
    main()
