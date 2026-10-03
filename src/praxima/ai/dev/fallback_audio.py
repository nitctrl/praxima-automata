"""Local test-only outage audio, rendered during setup rather than during an outage."""

import wave
from collections.abc import AsyncIterator
from pathlib import Path

from livekit import rtc

FALLBACK = {
    "en-IN": "This is a development test. Voice service is unavailable. Please try again later.",
    "hi-IN": "यह केवल एक परीक्षण है। आवाज़ की सेवा उपलब्ध नहीं है। कृपया बाद में फिर कोशिश करें।",
}


def load_audio(path: Path) -> tuple[int, bytes]:
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError("Invalid test fallback audio")
    with wave.open(str(path), "rb") as audio:
        rate = audio.getframerate()
        if (
            audio.getnchannels() != 1
            or audio.getsampwidth() != 2
            or rate not in {16000, 22050, 24000, 44100, 48000}
            or not 0 < audio.getnframes() <= rate * 20
        ):
            raise ValueError("Fallback must be bounded mono PCM16 WAV audio")
        data = audio.readframes(audio.getnframes())
        if len(data) != audio.getnframes() * 2:
            raise ValueError("Truncated fallback audio")
    return rate, data


async def frames(audio: tuple[int, bytes]) -> AsyncIterator[rtc.AudioFrame]:
    rate, data = audio
    size = (rate // 50) * 2
    for offset in range(0, len(data), size):
        chunk = data[offset : offset + size]
        yield rtc.AudioFrame(
            data=chunk, sample_rate=rate, num_channels=1, samples_per_channel=len(chunk) // 2
        )
