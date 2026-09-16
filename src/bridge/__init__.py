"""Telephony <-> LiveKit WebSocket bridge.

Kept deliberately separate from the agent: the agent only ever sees a normal
LiveKit participant, so swapping carriers never touches agent code.
"""

from .config import BridgeConfig, ConfigError
from .room import CallBridge, CallSocket

__all__ = ["BridgeConfig", "ConfigError", "CallBridge", "CallSocket"]
