"""
Fake Exotel carrier — exercises the bridge end to end without Exotel.
====================================================================

Speaks the Exotel Voicebot wire protocol at the bridge, streams caller audio
in real time, and reports what the agent sends back.

    # terminal 1
    uv run src/agent.py dev
    # terminal 2
    uv run python -m bridge.server
    # terminal 3
    uv run python tests/simulate_call.py            # 440 Hz tone
    uv run python tests/simulate_call.py speech.wav # mono 16-bit WAV
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000  # 20 ms of PCM16 mono


def tone(seconds: float, hz: float = 440.0) -> bytes:
    total = int(SAMPLE_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * n / SAMPLE_RATE)))
        for n in range(total)
    )


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit("WAV must be mono 16-bit PCM")
        if wav.getframerate() != SAMPLE_RATE:
            raise SystemExit(f"WAV must be {SAMPLE_RATE} Hz (got {wav.getframerate()})")
        return wav.readframes(wav.getnframes())


async def receive(ws: aiohttp.ClientWebSocketResponse, stats: dict) -> None:
    async for msg in ws:
        if msg.type is not aiohttp.WSMsgType.TEXT:
            continue
        event = json.loads(msg.data)
        kind = event.get("event")
        if kind == "media":
            audio = base64.b64decode(event["media"]["payload"])
            stats["frames"] += 1
            stats["bytes"] += len(audio)
            if stats["first"] is None:
                stats["first"] = time.monotonic()
                print("  <- first agent audio received")
            if len(audio) % 320:
                stats["misaligned"] += 1
        elif kind == "clear":
            print("  <- clear (barge-in flush)")


async def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", nargs="?", help="mono 16-bit 8 kHz WAV")
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--url", default=None)
    args = parser.parse_args()

    port = os.environ.get("BRIDGE_PORT", "8080")
    url = args.url or f"http://127.0.0.1:{port}/exotel?sample-rate={SAMPLE_RATE}"

    user = os.environ.get("BRIDGE_AUTH_USER", "")
    password = os.environ.get("BRIDGE_AUTH_PASSWORD", "")
    headers = {}
    if user and password:
        raw = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {raw}"

    pcm = load_wav(Path(args.audio)) if args.audio else tone(args.seconds)
    call_id = f"sim-{int(time.time())}"
    stats = {"frames": 0, "bytes": 0, "first": None, "misaligned": 0}

    print(f"connecting to {url}")
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.ws_connect(url, heartbeat=30) as ws:
            reader = asyncio.create_task(receive(ws, stats))

            await ws.send_str(json.dumps({"event": "connected"}))
            await ws.send_str(
                json.dumps(
                    {
                        "event": "start",
                        "sequence_number": 1,
                        "stream_sid": call_id,
                        "start": {
                            "stream_sid": call_id,
                            "call_sid": call_id,
                            "account_sid": "sim",
                            "from": "+919876500000",
                            "to": "+918041234567",
                            "custom_parameters": {},
                            "media_format": {
                                "encoding": "raw",
                                "sample_rate": str(SAMPLE_RATE),
                                "bit_rate": "16",
                            },
                        },
                    }
                )
            )
            print("  -> start sent; streaming caller audio in real time")

            started = time.monotonic()
            for index in range(0, len(pcm), FRAME_BYTES):
                frame = pcm[index : index + FRAME_BYTES]
                await ws.send_str(
                    json.dumps(
                        {
                            "event": "media",
                            "stream_sid": call_id,
                            "sequence_number": index // FRAME_BYTES + 2,
                            "media": {
                                "chunk": index // FRAME_BYTES,
                                "timestamp": str(index // 16),
                                "payload": base64.b64encode(frame).decode(),
                            },
                        }
                    )
                )
                # Pace to wall clock so the agent's VAD behaves as on a real call.
                target = started + (index / FRAME_BYTES + 1) * FRAME_MS / 1000
                await asyncio.sleep(max(0.0, target - time.monotonic()))

            print("  -> caller audio done; listening for agent reply (10s)")
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=10)
            except asyncio.TimeoutError:
                pass

            await ws.send_str(
                json.dumps(
                    {
                        "event": "stop",
                        "stream_sid": call_id,
                        "sequence_number": 9999,
                        "stop": {"call_sid": call_id, "reason": "callended"},
                    }
                )
            )
            reader.cancel()

    seconds = stats["bytes"] / (SAMPLE_RATE * 2)
    print("\n─── result ───")
    print(f"agent media frames : {stats['frames']}")
    print(f"agent audio        : {seconds:.2f}s ({stats['bytes']} bytes)")
    print(f"misaligned chunks  : {stats['misaligned']}  (must be 0)")

    if stats["frames"] == 0:
        print("\nFAIL: no audio came back from the agent.")
        return 1
    if stats["misaligned"]:
        print("\nFAIL: Exotel requires 320-byte aligned chunks.")
        return 1
    print("\nPASS: audio flowed in both directions.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
