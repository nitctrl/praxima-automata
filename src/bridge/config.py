"""Bridge configuration, resolved once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised at startup when required settings are missing."""


@dataclass(frozen=True)
class BridgeConfig:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str

    agent_name: str = "inbound-agent"
    room_prefix: str = "call-"

    host: str = "0.0.0.0"
    port: int = 8080

    #: HTTP Basic credentials the carrier must present. Empty disables the
    #: check — only acceptable when the socket is IP-allowlisted or local.
    auth_user: str = ""
    auth_password: str = ""

    #: Abandon the call if the carrier sends no `start` within this window.
    start_timeout: float = 10.0

    @property
    def auth_required(self) -> bool:
        return bool(self.auth_user and self.auth_password)

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        missing = [
            key
            for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
            if not os.environ.get(key)
        ]
        if missing:
            raise ConfigError(f"missing required env vars: {', '.join(missing)}")

        return cls(
            livekit_url=os.environ["LIVEKIT_URL"],
            livekit_api_key=os.environ["LIVEKIT_API_KEY"],
            livekit_api_secret=os.environ["LIVEKIT_API_SECRET"],
            agent_name=os.environ.get("BRIDGE_AGENT_NAME", "inbound-agent"),
            room_prefix=os.environ.get("BRIDGE_ROOM_PREFIX", "call-"),
            host=os.environ.get("BRIDGE_HOST", "0.0.0.0"),
            port=int(os.environ.get("BRIDGE_PORT", "8080")),
            auth_user=os.environ.get("BRIDGE_AUTH_USER", ""),
            auth_password=os.environ.get("BRIDGE_AUTH_PASSWORD", ""),
            start_timeout=float(os.environ.get("BRIDGE_START_TIMEOUT", "10")),
        )
