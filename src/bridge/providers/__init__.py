"""Telephony provider registry.

Add a carrier by implementing `TelephonyProvider` and registering its factory
here. Nothing else in the codebase needs to change.
"""

from __future__ import annotations

from typing import Callable, Mapping

from .base import (
    AudioReceived,
    CallEnded,
    CallStarted,
    DtmfReceived,
    Ignored,
    TelephonyEvent,
    TelephonyProvider,
)
from .exotel import ExotelProvider

#: Route segment -> factory producing a fresh per-call provider instance.
PROVIDERS: Mapping[str, Callable[[], TelephonyProvider]] = {
    "exotel": ExotelProvider,
}

__all__ = [
    "PROVIDERS",
    "TelephonyProvider",
    "TelephonyEvent",
    "CallStarted",
    "AudioReceived",
    "DtmfReceived",
    "CallEnded",
    "Ignored",
    "ExotelProvider",
]
