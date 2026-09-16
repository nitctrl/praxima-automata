"""
Provider-agnostic telephony protocol layer.
=============================================

Every telephony vendor (Exotel, Twilio, Plivo, Vonage...) speaks its own
WebSocket dialect. This module defines the *normalized* event vocabulary the
bridge understands, plus the `TelephonyProvider` interface a vendor adapter
must implement.

The bridge in `bridge/room.py` depends ONLY on this module — never on a
concrete vendor. Swapping Exotel for another carrier means writing one new
adapter; nothing else changes.

Audio convention across this boundary:
    raw PCM, signed 16-bit, little-endian, mono, at `sample_rate` Hz.
Vendor-specific encodings (base64, mu-law, etc.) stay inside the adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Mapping, Union

# ════════════════════════════════════════════════════════════════════
#  NORMALIZED EVENTS  (vendor → bridge)
# ════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CallStarted:
    """First meaningful event: the carrier has bridged a live caller."""

    call_id: str
    from_number: str | None = None
    to_number: str | None = None
    sample_rate: int = 8000
    custom: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AudioReceived:
    """Caller audio. `pcm16` is mono little-endian PCM at the call sample rate."""

    pcm16: bytes


@dataclass(frozen=True)
class DtmfReceived:
    digit: str


@dataclass(frozen=True)
class CallEnded:
    reason: str = "unknown"


@dataclass(frozen=True)
class Ignored:
    """Protocol housekeeping the bridge does not act on (keepalives, acks)."""

    kind: str = ""


TelephonyEvent = Union[CallStarted, AudioReceived, DtmfReceived, CallEnded, Ignored]


# ════════════════════════════════════════════════════════════════════
#  PROVIDER INTERFACE
# ════════════════════════════════════════════════════════════════════


class TelephonyProvider(ABC):
    """
    One instance per call. Adapters may hold per-call state (stream ids,
    sequence counters), so never share an instance between connections.
    """

    #: Short identifier, surfaced to the agent as a participant attribute.
    name: str = "unknown"

    #: Sample rate assumed before a CallStarted event refines it.
    default_sample_rate: int = 8000

    #: Outbound audio must be flushed in whole multiples of this many bytes.
    outbound_chunk_alignment: int = 2

    #: Minimum bytes to accumulate before sending audio back to the carrier.
    outbound_min_chunk: int = 2

    @abstractmethod
    def decode(self, message: str | bytes) -> TelephonyEvent:
        """Translate one raw WebSocket message into a normalized event."""

    @abstractmethod
    def encode_audio(self, pcm16: bytes) -> str | bytes:
        """Wrap agent audio into a vendor frame ready to send over the socket."""

    def encode_clear(self) -> str | bytes | None:
        """
        Ask the carrier to discard audio it has buffered but not yet played.
        Used for barge-in. Return None if the vendor has no such command.
        """
        return None
