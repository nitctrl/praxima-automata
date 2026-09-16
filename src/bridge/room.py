"""
Carrier <-> LiveKit room bridge.
================================

Responsibilities
  * Own one LiveKit room participant per phone call.
  * Pump caller audio into the room as a published audio track.
  * Pump the agent's audio back out to the carrier.

This module deliberately knows nothing about any specific carrier. It talks to
a `TelephonyProvider` (see `providers/base.py`) and to an abstract `CallSocket`,
so it is exercised in tests with an in-memory socket and no real WebSocket.

Barge-in note
  Audio is forwarded at the pace LiveKit delivers it (a live WebRTC track is
  inherently real-time), so at most one outbound chunk sits un-played at the
  carrier. When the agent is interrupted, playback therefore stops within
  roughly one chunk duration. `flush_playback()` is available if a carrier ever
  needs a harder, explicit flush.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import AsyncIterator, Protocol, runtime_checkable

from livekit import api, rtc

from .config import BridgeConfig
from .providers.base import (
    AudioReceived,
    CallEnded,
    CallStarted,
    DtmfReceived,
    TelephonyProvider,
)

logger = logging.getLogger("bridge.room")

#: Exotel rejects frames above 100 KB; keep a margin and stay 320-aligned.
_MAX_OUTBOUND_CHUNK = 32_000


@runtime_checkable
class CallSocket(Protocol):
    """Minimal duplex message socket. `aiohttp` and test doubles both satisfy it."""

    async def send(self, message: str | bytes) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


class CallBridge:
    """Runs exactly one call. Not reusable across connections."""

    def __init__(self, provider: TelephonyProvider, config: BridgeConfig) -> None:
        self._provider = provider
        self._config = config

        self._room: rtc.Room | None = None
        self._source: rtc.AudioSource | None = None
        self._sample_rate = provider.default_sample_rate

        self._socket: CallSocket | None = None
        self._forward_task: asyncio.Task | None = None
        self._forwarding = False

        self._room_name: str | None = None
        self._closed = False

    # ── lifecycle ───────────────────────────────────────────────────
    async def run(self, socket: CallSocket) -> None:
        self._socket = socket
        started = False
        try:
            async for raw in socket:
                event = self._provider.decode(raw)

                if isinstance(event, CallStarted):
                    if started:
                        logger.warning("duplicate start event ignored")
                        continue
                    started = True
                    await self._on_start(event)

                elif isinstance(event, AudioReceived):
                    await self._on_audio(event)

                elif isinstance(event, DtmfReceived):
                    logger.info("dtmf digit=%s", event.digit)

                elif isinstance(event, CallEnded):
                    logger.info("carrier ended call: %s", event.reason)
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bridge loop failed")
        finally:
            await self.aclose()

    async def _on_start(self, event: CallStarted) -> None:
        self._sample_rate = event.sample_rate or self._provider.default_sample_rate
        # Room names land in logs and traces, so key on the carrier's call id
        # rather than the caller's phone number.
        self._room_name = f"{self._config.room_prefix}{self._provider.name}-{event.call_id}"

        logger.info(
            "call start id=%s from=%s to=%s rate=%d room=%s",
            event.call_id,
            _redact(event.from_number),
            event.to_number,
            self._sample_rate,
            self._room_name,
        )

        await self._dispatch_agent(event)
        await self._connect_room(event)

    async def _dispatch_agent(self, event: CallStarted) -> None:
        """Explicit dispatch — the agent registers under a name, so it will not
        auto-join. Doing this first lets the agent be ready before we connect."""
        metadata = json.dumps(
            {
                "provider": self._provider.name,
                "call_id": event.call_id,
                "from": event.from_number,
                "to": event.to_number,
                "custom": dict(event.custom),
            }
        )
        async with api.LiveKitAPI(
            url=_http_url(self._config.livekit_url),
            api_key=self._config.livekit_api_key,
            api_secret=self._config.livekit_api_secret,
        ) as lkapi:
            await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=self._config.agent_name,
                    room=self._room_name,
                    metadata=metadata,
                )
            )
        logger.info("dispatched agent=%s", self._config.agent_name)

    async def _connect_room(self, event: CallStarted) -> None:
        assert self._room_name

        identity = f"caller-{event.call_id}-{uuid.uuid4().hex[:6]}"
        token = (
            api.AccessToken(self._config.livekit_api_key, self._config.livekit_api_secret)
            .with_identity(identity)
            .with_name("Caller")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=self._room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=False,
                )
            )
            # Generic, carrier-neutral hints so the agent can adapt its audio
            # pipeline without knowing which vendor is upstream.
            .with_attributes(
                {
                    "telephony": "true",
                    "telephony.provider": self._provider.name,
                    "telephony.call_id": event.call_id,
                    "sip.phoneNumber": event.from_number or "",
                }
            )
            .to_jwt()
        )

        room = rtc.Room()
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("disconnected", lambda *_: asyncio.create_task(self.aclose()))
        await room.connect(self._config.livekit_url, token)
        self._room = room

        source = rtc.AudioSource(self._sample_rate, 1)
        track = rtc.LocalAudioTrack.create_audio_track("caller", source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await room.local_participant.publish_track(track, options)
        self._source = source

        logger.info("joined room=%s identity=%s", self._room_name, identity)

    # ── caller -> LiveKit ───────────────────────────────────────────
    async def _on_audio(self, event: AudioReceived) -> None:
        if self._source is None:
            # Audio before `start`; nowhere to put it yet.
            return
        frame = rtc.AudioFrame(
            data=event.pcm16,
            sample_rate=self._sample_rate,
            num_channels=1,
            samples_per_channel=len(event.pcm16) // 2,
        )
        await self._source.capture_frame(frame)

    # ── LiveKit -> caller ───────────────────────────────────────────
    def _on_track_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO or self._forwarding:
            return
        self._forwarding = True
        logger.info("forwarding audio from %s", participant.identity)
        self._forward_task = asyncio.create_task(self._forward_audio(track))

    async def _forward_audio(self, track: rtc.Track) -> None:
        provider = self._provider
        align = max(provider.outbound_chunk_alignment, 2)
        minimum = max(provider.outbound_min_chunk, align)

        stream = rtc.AudioStream(track, sample_rate=self._sample_rate, num_channels=1)
        buffer = bytearray()
        try:
            async for frame_event in stream:
                buffer += frame_event.frame.data.tobytes()

                while len(buffer) >= minimum:
                    size = min((len(buffer) // align) * align, _MAX_OUTBOUND_CHUNK)
                    if size < minimum:
                        break
                    chunk = bytes(buffer[:size])
                    del buffer[:size]
                    await self._send(provider.encode_audio(chunk))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audio forwarding stopped")
        finally:
            await stream.aclose()

    async def flush_playback(self) -> None:
        """Drop audio the carrier has buffered but not yet played (barge-in)."""
        if message := self._provider.encode_clear():
            await self._send(message)

    async def _send(self, message: str | bytes) -> None:
        if self._closed or self._socket is None:
            return
        try:
            await self._socket.send(message)
        except Exception:
            logger.debug("socket send failed; carrier likely hung up")
            self._closed = True

    # ── teardown ────────────────────────────────────────────────────
    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._forward_task:
            self._forward_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._forward_task

        if self._source:
            await self._source.aclose()
        if self._room:
            await self._room.disconnect()

        logger.info("bridge closed room=%s", self._room_name)


def _http_url(url: str) -> str:
    """LiveKit's REST client needs http(s), but config carries the ws(s) URL."""
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    return url


def _redact(number: str | None) -> str:
    """Keep only the last 4 digits of a caller number in logs."""
    if not number:
        return "unknown"
    return f"***{number[-4:]}" if len(number) > 4 else "***"
