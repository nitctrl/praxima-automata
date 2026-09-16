"""
Exotel Voicebot (bidirectional stream) adapter.
===============================================

Protocol reference:
  https://support.exotel.com/support/solutions/articles/3000108630

Wire format
  * Every message is a JSON string.
  * Audio payloads are base64-encoded raw/slin: 16-bit, mono, little-endian
    PCM. Rate defaults to 8 kHz and is selectable via the `sample-rate` query
    parameter on the Voicebot applet URL (8000 / 16000 / 24000).
  * Outbound chunks must be a multiple of 320 bytes, at least 3.2 KB and at
    most 100 KB. Undersized chunks cause audible gaps because the platform
    stalls ~20 ms waiting for the remainder.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging

from .base import (
    AudioReceived,
    CallEnded,
    CallStarted,
    DtmfReceived,
    Ignored,
    TelephonyEvent,
    TelephonyProvider,
)

logger = logging.getLogger("bridge.exotel")

#: Exotel mandates 320-byte alignment on outbound media.
_ALIGNMENT = 320

#: Documented minimum outbound chunk (3.2 KB).
_MIN_CHUNK = 3200

_SUPPORTED_RATES = (8000, 16000, 24000)


class ExotelProvider(TelephonyProvider):
    name = "exotel"
    default_sample_rate = 8000
    outbound_chunk_alignment = _ALIGNMENT
    outbound_min_chunk = _MIN_CHUNK

    def __init__(self) -> None:
        self._stream_sid: str | None = None
        self._out_sequence = 0

    # ── inbound ─────────────────────────────────────────────────────
    def decode(self, message: str | bytes) -> TelephonyEvent:
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("discarding non-JSON frame")
            return Ignored("malformed")

        if not isinstance(payload, dict):
            return Ignored("malformed")

        event = payload.get("event")

        # `stream_sid` accompanies every post-start message; latch it so we
        # can echo it back on outbound frames.
        if sid := payload.get("stream_sid"):
            self._stream_sid = sid

        if event == "start":
            return self._decode_start(payload)
        if event == "media":
            return self._decode_media(payload)
        if event == "dtmf":
            digit = str((payload.get("dtmf") or {}).get("digit", ""))
            return DtmfReceived(digit=digit) if digit else Ignored("dtmf")
        if event == "stop":
            reason = str((payload.get("stop") or {}).get("reason", "stopped"))
            return CallEnded(reason=reason)
        if event in ("connected", "mark", "clear"):
            return Ignored(event)

        return Ignored(str(event))

    def _decode_start(self, payload: dict) -> TelephonyEvent:
        start = payload.get("start") or {}
        self._stream_sid = start.get("stream_sid") or self._stream_sid

        media_format = start.get("media_format") or {}
        rate = self._coerce_rate(media_format.get("sample_rate"))

        custom = start.get("custom_parameters") or {}
        if not isinstance(custom, dict):
            custom = {}

        return CallStarted(
            call_id=str(start.get("call_sid") or self._stream_sid or "unknown"),
            from_number=start.get("from") or None,
            to_number=start.get("to") or None,
            sample_rate=rate,
            custom={str(k): str(v) for k, v in custom.items()},
        )

    def _coerce_rate(self, raw: object) -> int:
        """Exotel sends sample_rate as a string in some firmware versions."""
        try:
            rate = int(str(raw))
        except (TypeError, ValueError):
            return self.default_sample_rate
        if rate not in _SUPPORTED_RATES:
            logger.warning("unexpected sample rate %s, using it verbatim", rate)
        return rate or self.default_sample_rate

    def _decode_media(self, payload: dict) -> TelephonyEvent:
        raw = (payload.get("media") or {}).get("payload")
        if not raw:
            return Ignored("empty-media")
        try:
            pcm = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            logger.warning("discarding undecodable media payload")
            return Ignored("bad-media")
        # A trailing odd byte would desynchronise every subsequent sample.
        if len(pcm) % 2:
            pcm = pcm[:-1]
        return AudioReceived(pcm16=pcm) if pcm else Ignored("empty-media")

    # ── outbound ────────────────────────────────────────────────────
    def encode_audio(self, pcm16: bytes) -> str:
        self._out_sequence += 1
        return json.dumps(
            {
                "event": "media",
                "stream_sid": self._stream_sid,
                "sequence_number": self._out_sequence,
                "media": {"payload": base64.b64encode(pcm16).decode("ascii")},
            }
        )

    def encode_clear(self) -> str | None:
        if not self._stream_sid:
            return None
        self._out_sequence += 1
        return json.dumps(
            {
                "event": "clear",
                "stream_sid": self._stream_sid,
                "sequence_number": self._out_sequence,
            }
        )
