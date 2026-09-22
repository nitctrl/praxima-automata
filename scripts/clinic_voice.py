"""Local microphone test only. Use fictional details; never an inbound worker."""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import dotenv_values
from livekit.agents import AgentSession, JobContext, JobProcess, WorkerOptions, cli
from livekit.agents.voice.events import MetricsCollectedEvent
from livekit.plugins import sarvam, silero

from clinic.activation import load_development
from clinic.db import RuntimeDatabase
from clinic.dev_conversation import DevelopmentConversation
from clinic.dev_voice import DevelopmentVoiceAgent
from clinic.fallback_audio import FALLBACK, load_audio
from clinic.resolver import ClinicResolver, InboundDestination, PostgresResolutionRepository
from clinic.sessions import CallOrchestrator, CallSessionService
from clinic.settings import DatabaseSettings
from clinic.usage import UsageCollector

ROOT = Path(__file__).resolve().parents[1]


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    # The CLI's console mode creates this fake job without registering a worker.
    # No participant metadata or caller ID is accepted as a clinic selector.
    if ctx.job.room.name != "console-room":
        ctx.shutdown("development_console_only")
        return
    values = dotenv_values(ROOT / ".env")
    project = values.get("SUPABASE_PROJECT_REF") or ""
    clinic, cipher = load_development(ROOT, project)
    runtime = dotenv_values(ROOT / ".env.runtime")
    database = RuntimeDatabase(
        DatabaseSettings.validate(runtime.get("DATABASE_URL") or "", project)
    )
    await database.open()
    call: CallOrchestrator | None = None
    collector: UsageCollector | None = None
    session: AgentSession[Any] = AgentSession(
        preemptive_generation=False, resume_false_interruption=False
    )
    voices = {
        language: sarvam.TTS(
            model="bulbul:v3",
            target_language_code=language,
            speaker=values.get("SARVAM_TTS_SPEAKER") or "shubh",
            api_key=values.get("SARVAM_API_KEY"),
        )
        for language in FALLBACK
    }
    recognizer = sarvam.STT(
        model="saaras:v3", language="unknown", api_key=values.get("SARVAM_API_KEY")
    )
    stop_task: asyncio.Task[None] | None = None
    finished = asyncio.Event()
    closed = False

    async def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        finished.set()
        if stop_task and stop_task is not asyncio.current_task():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        await session.aclose()
        if collector:
            await collector.close()
        if call:
            await call.close()
        for voice in voices.values():
            await voice.aclose()
        await recognizer.aclose()
        await database.close()

    ctx.add_shutdown_callback(close)
    try:
        scope = await ClinicResolver(PostgresResolutionRepository(database)).resolve(
            InboundDestination("+12025550101", "fixture-only-not-a-live-trunk", "test")
        )
        if scope.clinic_id != clinic:
            raise ValueError("Fictional scope mismatch")
        context = await CallSessionService(database).start(
            scope,
            provider="test",
            account="local-development",
            call_id=str(uuid4()),
            room_id=str(uuid4()),
            is_test=True,
        )
        call = await CallOrchestrator.load(CallSessionService(database), context, cipher)
        if set(call.snapshot.supported_languages) - set(FALLBACK):
            raise ValueError("Only English/Hindi test audio is configured")
        collector = UsageCollector(call)
        fallback = {
            language: load_audio(ROOT / ".clinic-dev-audio" / f"{language}.wav")
            for language in FALLBACK
        }
        agent = DevelopmentVoiceAgent(
            DevelopmentConversation(call),
            speech_to_text=recognizer,
            voices=voices,
            detector=ctx.proc.userdata["vad"],
            fallback=fallback,
        )

        def metrics(event: MetricsCollectedEvent) -> None:
            if collector:
                collector.submit(event.metrics)

        session.on("metrics_collected", metrics)
        # The custom bilingual tts_node drives these engines directly.
        for voice in voices.values():
            voice.on("metrics_collected", collector.submit)
        session.on("close", lambda event: finished.set())
        session.on("error", lambda event: agent.provider_failed())
        recognizer.on("error", lambda event: agent.provider_failed())
        for voice in voices.values():
            voice.on("error", lambda event: agent.provider_failed())

        async def stop_media() -> None:
            assert call is not None
            await call.stop_event.wait()
            await agent.play_fallback()
            await session.aclose()
            finished.set()
            ctx.shutdown("development_call_ended")

        call.start_watchdog()
        stop_task = asyncio.create_task(stop_media(), name="clinic-stop-media")
        await asyncio.wait_for(session.start(agent=agent, record=False), 30)
        await finished.wait()
    except Exception:
        ctx.shutdown("development_startup_failed")
        raise RuntimeError("Development voice unavailable; verify setup and publication") from None
    finally:
        await close()


if __name__ == "__main__":
    if sys.argv[1:] not in [["console"], ["--help"]]:
        raise SystemExit("This entrypoint supports console only. It cannot register for SIP calls.")
    # No migration credentials are copied into the voice process environment.
    values = dotenv_values(ROOT / ".env")
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if values.get(name):
            os.environ.setdefault(name, values[name] or "")
    # The SDK logs recognized text and provider exceptions below this threshold.
    # This test process disables those logs even when the console resets logger levels.
    logging.disable(logging.CRITICAL)
    cli.run_app(
        WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name="clinic-dev-agent")
    )
