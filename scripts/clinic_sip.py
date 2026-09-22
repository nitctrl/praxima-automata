"""Controlled fictional phone test on the explicitly authorized single SIP number."""

import asyncio
import logging
import sys
from functools import partial
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from livekit import api, rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RoomInputOptions,
    RoomOutputOptions,
    cli,
)
from livekit.agents.voice.events import MetricsCollectedEvent
from livekit.plugins import noise_cancellation, sarvam, silero

from clinic.activation import load_development
from clinic.db import RuntimeDatabase
from clinic.dev_voice import DevelopmentVoiceAgent
from clinic.fallback_audio import FALLBACK, load_audio
from clinic.resolver import ClinicResolver, PostgresResolutionRepository
from clinic.sessions import CallContext, CallOrchestrator, CallSessionService
from clinic.settings import DatabaseSettings
from clinic.sip_test import CLINIC, LIVEKIT_URL, PROJECT, WORKER, SipTestConversation, preflight
from clinic.sip_test import ingress as parse_ingress
from clinic.usage import UsageCollector
from clinic.voice_errors import Stage, recoverable, report

ROOT = Path(__file__).resolve().parents[1]
# Also applied in spawned/forkserver job processes, not just the CLI parent.
logging.disable(logging.CRITICAL)


def database_settings() -> DatabaseSettings:
    runtime = dotenv_values(ROOT / ".env.runtime")
    return DatabaseSettings.validate(runtime.get("DATABASE_URL") or "", PROJECT)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    # This worker is dispatched by a single-number individual-room rule, never console.
    if not ctx.room.name.startswith("call-"):
        ctx.shutdown("invalid_test_room")
        return
    values = dotenv_values(ROOT / ".env")
    _, cipher = load_development(ROOT, PROJECT)
    database = RuntimeDatabase(database_settings())
    service = CallSessionService(database)
    context: CallContext | None = None
    call: CallOrchestrator | None = None
    collector: UsageCollector | None = None
    session: AgentSession[Any] | None = None
    voices: dict[str, Any] = {}
    recognizer: Any = None
    agent: DevelopmentVoiceAgent | None = None
    finished = asyncio.Event()
    closed = False
    admitted = False
    failed = False
    stop_task: asyncio.Task[None] | None = None

    async def end_room() -> None:
        # Only delete our dedicated admitted call room, never unrelated/rejected rooms.
        if admitted:
            try:
                await asyncio.wait_for(
                    ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name)), 10
                )
            except Exception as exc:
                report("hangup", exc)
                print("Clinic SIP test: room hangup could not be verified.", flush=True)

    async def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        finished.set()
        if stop_task and stop_task is not asyncio.current_task():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        # Independently bounded cleanup: media failure must not skip durable finalization.
        if context:
            try:
                if failed:
                    try:
                        await asyncio.wait_for(service.note(context, "voice", "failed"), 5)
                    except Exception:
                        pass
                if call:
                    await call.close()
                else:
                    await asyncio.wait_for(service.event(context, "ended"), 5)
                print(f"Clinic SIP test: finalized call {context.session_id}.", flush=True)
            except Exception:
                print("Clinic SIP test: finalization unavailable; reconcile required.", flush=True)
        if session:
            try:
                await asyncio.wait_for(session.aclose(), 10)
            except Exception:
                pass
        if collector:
            try:
                await collector.close()
            except Exception:
                pass
        await end_room()
        for engine in [*voices.values(), recognizer]:
            if engine:
                try:
                    await asyncio.wait_for(engine.aclose(), 5)
                except Exception:
                    pass
        await database.close()

    ctx.add_shutdown_callback(close)
    try:
        await asyncio.wait_for(ctx.connect(), 20)
        participant = await asyncio.wait_for(ctx.wait_for_participant(), 30)
        route = parse_ingress(participant.kind, participant.attributes)
        admitted = True
        await database.open()
        resolver = ClinicResolver(PostgresResolutionRepository(database))
        scope = await resolver.resolve(route.destination)
        if scope.clinic_id != CLINIC:
            raise ValueError("Clinic mismatch")
        context = await service.start(
            scope, provider="plivo", account="livekit:receptionist-00",
            call_id=route.call_id, room_id=ctx.room.name, is_test=True,
        )
        await service.note(context, "connected", "success")
        print(f"Clinic SIP test: created call {context.session_id}.", flush=True)
        call = await CallOrchestrator.load(service, context, cipher)
        if set(call.snapshot.supported_languages) - set(FALLBACK):
            raise ValueError("Unsupported language")
        session = AgentSession(preemptive_generation=False, resume_false_interruption=False)
        voices = {
            language: sarvam.TTS(
                model="bulbul:v3", target_language_code=language,
                speaker=values.get("SARVAM_TTS_SPEAKER") or "shubh",
                api_key=values.get("SARVAM_API_KEY"),
            ) for language in FALLBACK
        }
        recognizer = sarvam.STT(
            model="saaras:v3", language="unknown", api_key=values.get("SARVAM_API_KEY")
        )
        agent = DevelopmentVoiceAgent(
            SipTestConversation(call), speech_to_text=recognizer, voices=voices,
            detector=ctx.proc.userdata["vad"],
            fallback={lang: load_audio(ROOT / ".clinic-dev-audio" / f"{lang}.wav")
                      for lang in FALLBACK},
        )
        collector = UsageCollector(call)

        def metrics(event: MetricsCollectedEvent) -> None:
            if collector:
                collector.submit(event.metrics)

        def provider_failed(event: Any, *, stage: Stage = "session") -> None:
            nonlocal failed
            report(stage, event)
            if recoverable(event):
                return  # Let the SDK complete its bounded retries before failing closed.
            failed = True
            if agent:
                agent.provider_failed()

        def disconnected(remote: rtc.RemoteParticipant) -> None:
            if remote.identity == participant.identity:
                finished.set()

        ctx.room.on("participant_disconnected", disconnected)
        ctx.room.on("disconnected", lambda *args: finished.set())
        session.on("metrics_collected", metrics)
        session.on("close", lambda event: finished.set())
        session.on("error", provider_failed)
        recognizer.on("error", partial(provider_failed, stage="stt"))
        for voice in voices.values():
            voice.on("metrics_collected", collector.submit)
            voice.on("error", partial(provider_failed, stage="tts"))

        async def stop_media() -> None:
            nonlocal failed
            assert call is not None and agent is not None
            await call.stop_event.wait()
            failed = not call._closed
            await agent.play_fallback()
            finished.set()

        call.start_watchdog()
        stop_task = asyncio.create_task(stop_media(), name="clinic-sip-stop")
        await asyncio.wait_for(session.start(
            agent=agent, room=ctx.room, record=False,
            room_output_options=RoomOutputOptions(transcription_enabled=False),
            room_input_options=RoomInputOptions(
                participant_identity=participant.identity,
                participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_SIP],
                text_enabled=False, video_enabled=False,
                noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        ), 30)
        # If the caller left during startup, do not wait for a missed event.
        if participant.identity not in ctx.room.remote_participants:
            finished.set()
        await finished.wait()
    except Exception as exc:
        failed = True
        report("startup", exc)
        name = type(exc).__name__
        print(f"Clinic SIP test unavailable ({name}); details suppressed.", flush=True)
        if agent:
            await agent.play_fallback()
    finally:
        await close()
        ctx.shutdown("clinic_sip_test_ended")


def main() -> None:
    if sys.argv[1:] not in [["start"], ["dev"], ["--help"]]:
        raise SystemExit("Use start or dev for the authorized fictional SIP test; no console.")
    logging.disable(logging.CRITICAL)  # SDK/provider logs can contain caller text and credentials.
    values = dotenv_values(ROOT / ".env")
    if values.get("LIVEKIT_URL") != LIVEKIT_URL or values.get("SUPABASE_PROJECT_REF") != PROJECT:
        raise SystemExit("Controlled SIP test project mismatch.")
    try:
        asyncio.run(preflight(ROOT))
    except Exception as exc:
        name = type(exc).__name__
        raise SystemExit(f"SIP preflight failed ({name}); verify activation.") from None
    server = AgentServer(
        ws_url=LIVEKIT_URL, api_key=values.get("LIVEKIT_API_KEY"),
        api_secret=values.get("LIVEKIT_API_SECRET"), setup_fnc=prewarm,
        num_idle_processes=1, host="127.0.0.1", port=8082,
        initialize_process_timeout=45, shutdown_process_timeout=45,
    )
    server.rtc_session(entrypoint, agent_name=WORKER)
    server.on("worker_registered", lambda *args: print(
        "Clinic SIP test worker REGISTERED; awaiting authorized calls.", flush=True,
    ))
    cli.run_app(server)


if __name__ == "__main__":
    main()