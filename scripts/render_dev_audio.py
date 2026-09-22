"""Generate fictional test outage audio once. Never overwrite an existing reviewed file."""

import argparse
import asyncio
import logging
import os
import wave
from pathlib import Path

import aiohttp
from dotenv import dotenv_values
from livekit.plugins import sarvam

from clinic.activation import development_settings, load_development
from clinic.fallback_audio import FALLBACK, load_audio

ROOT = Path(__file__).resolve().parents[1]


async def render(project: str) -> None:
    development_settings(ROOT, project)
    load_development(ROOT, project)
    values = dotenv_values(ROOT / ".env")
    directory = ROOT / ".clinic-dev-audio"
    directory.mkdir(mode=0o700, exist_ok=True)
    for language, text in FALLBACK.items():
        path = directory / f"{language}.wav"
        if path.exists():
            load_audio(path)
            continue
        http_session = aiohttp.ClientSession()
        engine = sarvam.TTS(
            model="bulbul:v3",
            target_language_code=language,
            speaker=values.get("SARVAM_TTS_SPEAKER") or "shubh",
            api_key=values.get("SARVAM_API_KEY"),
            http_session=http_session,
        )
        try:
            chunks = bytearray()
            async with engine.stream() as stream:
                stream.push_text(text)
                stream.end_input()
                async for event in stream:
                    chunks.extend(event.frame.data.tobytes())
                    if len(chunks) > engine.sample_rate * 2 * 20:
                        raise ValueError("Test fallback exceeds its duration limit")
            if not chunks:
                raise ValueError("No fallback audio returned")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output, wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(engine.sample_rate)
                wav.writeframes(chunks)
            load_audio(path)
            print(f"Cached {language} development fallback. Review before any test call.")
        finally:
            await engine.aclose()
            await http_session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-development-project", required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(asyncio.wait_for(render(args.confirm_development_project), 60))
    except Exception:
        raise SystemExit("Test audio generation failed; provider details suppressed.") from None


if __name__ == "__main__":
    main()
