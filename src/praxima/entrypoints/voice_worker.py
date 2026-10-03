"""
Voice AI agent: LiveKit Agents + Sarvam STT/TTS + Google Gemini.

Call path: Plivo -> SIP trunk -> LiveKit -> this worker (agent_name "inbound-agent").

Around the model, the call is handled deterministically (`runtime/call_flow.py`):
  - every caller turn is safety-classified first: emergencies get the published emergency
    message without the model; medical or prompt-injection turns get a guard instruction
  - knowledge loads in parallel with audio; a generic greeting covers a slow load
  - no knowledge (unknown number, untrusted trunk, database down): a fixed message, hang up
  - silence: "are you still there?", then goodbye; a maximum call duration; model errors
    ask the caller to repeat
  - the voice follows the caller's detected language (confident switches only)
The tenant comes from trusted SIP ingress only (called number + trunk), never caller ID.
"""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from time import monotonic
from typing import Any

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    JobContext,
    JobProcess,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
from livekit.agents.llm import ChatContext, ChatMessage, StopResponse
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import google, noise_cancellation, sarvam, silero

from praxima.dev.sip_test import ingress as clinic_ingress
from praxima.runtime import call_flow as flow
from praxima.runtime.policy.safety import classify
from praxima.runtime.release.knowledge import load_release_knowledge
from praxima.runtime.tools.agent_knowledge import AgentKnowledge, load_agent_knowledge

# ── Turn detector: optional ─────────────────────────────────────────
try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
    TURN_DETECTOR_AVAILABLE = True
except ImportError:
    TURN_DETECTOR_AVAILABLE = False

load_dotenv()

logger = logging.getLogger("voice-agent")
logger.setLevel(logging.INFO)


def _emergency_message(knowledge: Any) -> str:
    """The published emergency wording: release (agent) or legacy (clinic) snapshot."""
    snapshot = getattr(knowledge, "snapshot", None)
    agent = getattr(snapshot, "agent", None)
    return (
        getattr(agent, "emergency_message", None)
        or getattr(snapshot, "emergency_message", None)
        or flow.DEFAULT_EMERGENCY_MESSAGE
    )


# ════════════════════════════════════════════════════════════════════
#  VOICE AGENT — all config lives HERE, not on AgentSession
# ════════════════════════════════════════════════════════════════════

class VoiceAgent(Agent):
    def __init__(
        self,
        *,
        preloaded_vad=None,
        turn_detector=None,
        telephony: bool = False,
    ) -> None:
        # Knowledge arrives after the session starts (loaded in parallel with audio).
        self.clinic_knowledge: Any = None
        super().__init__(
            instructions=flow.LOADING_INSTRUCTIONS,
            tools=[],
            # "unknown" lets Saaras detect the language of every utterance.
            stt=sarvam.STT(
                model="saaras:v3",
                language=os.environ.get("SARVAM_STT_LANGUAGE", "unknown"),
                api_key=os.environ.get("SARVAM_API_KEY"),
            ),
            llm=google.LLM(
                model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                api_key=os.environ.get("GOOGLE_API_KEY"),
                temperature=0,
            ),
            tts=sarvam.TTS(
                model=os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3"),
                target_language_code=os.environ.get("SARVAM_TTS_LANGUAGE", "hi-IN"),
                speaker=os.environ.get("SARVAM_TTS_SPEAKER", "shubh"),
                api_key=os.environ.get("SARVAM_API_KEY"),
            ),
            vad=preloaded_vad,
            turn_detection=turn_detector,
            # PSTN adds codec + jitter latency vs WebRTC, so endpointing
            # is relaxed on telephony calls to avoid cutting callers off.
            allow_interruptions=True,
            min_endpointing_delay=0.45 if telephony else 0.21,
            max_endpointing_delay=1.2 if telephony else 0.75,
            min_consecutive_speech_delay=0.3,
        )

    def speak(self, text: str, *, interruptible: bool = True) -> Any:
        """Fixed speech without the model; points the voice at the text's script first."""
        if self.tts is not None:
            current = str(getattr(getattr(self.tts, "_opts", None), "target_language_code", ""))
            wanted = flow.speech_language(text, current)
            if wanted != current and wanted in flow.SARVAM_TTS_LANGUAGES:
                self.tts.update_options(target_language_code=wanted)
        return self.session.say(text, allow_interruptions=interruptible)

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        """Deterministic safety routing before the model sees the caller's turn."""
        decision = classify(new_message.text_content or "")
        if decision.route not in {"emergency", "medical", "injection"}:
            return
        recorder = getattr(self.clinic_knowledge, "recorder", None)
        if recorder is not None:  # content-free: which route, never the words
            recorder.tool_used("safety_router", decision.route)
        if decision.route == "emergency":
            self.speak(_emergency_message(self.clinic_knowledge))
            logger.warning("Safety route: emergency")
            raise StopResponse()
        turn_ctx.add_message(
            role="system",
            content=flow.MEDICAL_GUARD if decision.route == "medical" else flow.INJECTION_GUARD,
        )
        logger.info("Safety route: %s", decision.route)


# ════════════════════════════════════════════════════════════════════
#  LIFECYCLE
# ════════════════════════════════════════════════════════════════════

def prewarm(proc: JobProcess):
    """Pre-load heavy models once per worker process."""
    logger.info("Pre-warming: loading Silero VAD")
    proc.userdata["vad"] = silero.VAD.load()
    logger.info("VAD loaded")


async def _load_knowledge(participant: Any, telephony: bool) -> Any:
    """The call's knowledge, from trusted ingress only. Returns an object with `snapshot`."""
    sip = participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    if os.environ.get("PRAXIMA_VOICE_SOURCE", "legacy").strip().lower() == "release":
        if sip:
            ingress = flow.sip_ingress(participant.attributes)
            if ingress is None:
                logger.warning("SIP ingress rejected: malformed trusted attributes")
                return await load_release_knowledge("")
            knowledge = await load_release_knowledge(ingress.called_number, ingress.trunk_id)
        elif not telephony:
            # Console tests stand in for a call to PRAXIMA_CONSOLE_NUMBER.
            knowledge = await load_release_knowledge(os.environ.get("PRAXIMA_CONSOLE_NUMBER", ""))
        else:
            # Bridged calls carry no trusted called number.
            knowledge = await load_release_knowledge("")
        logger.info("Agent release %s", knowledge.describe())
        if knowledge.snapshot is not None:
            # Record the call (content-free); closed at hang-up.
            await knowledge.start_call(
                called_number=ingress.called_number if sip else
                os.environ.get("PRAXIMA_CONSOLE_NUMBER", ""),
                is_sip=sip,
                attributes=participant.attributes,
            )
        return knowledge
    # Legacy single-clinic development knowledge. Never expose it on another SIP route.
    authorized = not telephony
    if telephony:
        try:
            clinic_ingress(participant.kind, participant.attributes)
            authorized = True
        except LookupError:
            pass
    if not authorized:
        knowledge = AgentKnowledge(None)
        knowledge.reason = "unknown_number"
        return knowledge
    knowledge = await load_agent_knowledge(Path(__file__).resolve().parents[3])
    logger.info("Clinic knowledge %s", "loaded" if knowledge.snapshot else "unavailable")
    return knowledge


async def entrypoint(ctx: JobContext):
    started = monotonic()
    logger.info("New job room=%s", ctx.room.name)
    await ctx.connect()
    # On a phone call the SIP participant is bridged after the agent joins;
    # greeting before that clips the first words.
    participant = await ctx.wait_for_participant()
    telephony = (
        participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        or participant.attributes.get("telephony") == "true"
    )
    caller = participant.attributes.get("sip.phoneNumber", "")
    logger.info(
        "Participant joined telephony=%s caller=%s",
        telephony, "***" + caller[-4:] if len(caller) > 4 else "unknown",
    )

    # Load knowledge in parallel with audio startup; the greeting waits only briefly.
    loading: asyncio.Task[Any] = asyncio.create_task(_load_knowledge(participant, telephony))

    turn_detector = None
    if TURN_DETECTOR_AVAILABLE:
        try:
            turn_detector = MultilingualModel()
        except Exception:
            logger.warning("Turn detector failed; using VAD-only")

    session = AgentSession(user_away_timeout=flow.AWAY_SECONDS)
    voice_agent = VoiceAgent(
        preloaded_vad=ctx.proc.userdata["vad"],
        turn_detector=turn_detector,
        telephony=telephony,
    )
    ending = False
    background: set[asyncio.Task[Any]] = set()

    def spawn(work: Any) -> None:
        task = asyncio.create_task(work)
        background.add(task)
        task.add_done_callback(background.discard)

    async def hang_up(message: str) -> None:
        """Say a fixed message, then end the call for everyone."""
        nonlocal ending
        if ending:
            return
        ending = True
        with contextlib.suppress(Exception):
            handle = voice_agent.speak(message, interruptible=False)
            await asyncio.wait_for(handle.wait_for_playout(), flow.PLAYOUT_TIMEOUT_SECONDS)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ctx.delete_room(), 5)
        ctx.shutdown("call ended by agent")

    # ── Language: follow the caller only on confident, supported switches ──
    languages = flow.LanguageTracker(set(flow.SARVAM_TTS_LANGUAGES))

    def match_tts_language(ev) -> None:
        if not ev.is_final or voice_agent.tts is None:
            return
        wanted = languages.decide(str(ev.language or ""), ev.transcript or "")
        if wanted:
            voice_agent.tts.update_options(target_language_code=wanted)

    session.on("user_input_transcribed", match_tts_language)

    # ── Silence: ask once, then end politely ───────────────────────────
    away_check: list[asyncio.TimerHandle] = []

    def on_user_state(ev) -> None:
        for handle in away_check:
            handle.cancel()
        away_check.clear()
        if ev.new_state != "away" or ending:
            return
        with contextlib.suppress(Exception):
            voice_agent.speak(flow.STILL_THERE_MESSAGE)

        def still_away() -> None:
            if session.user_state == "away":
                logger.info("Caller silent; ending call")
                spawn(hang_up(flow.GOODBYE_MESSAGE))

        away_check.append(
            asyncio.get_running_loop().call_later(
                flow.AWAY_SECONDS + flow.AWAY_GRACE_SECONDS, still_away
            )
        )

    session.on("user_state_changed", on_user_state)

    # ── Provider failures never leave the caller in silence ─────────────
    def on_error(ev) -> None:
        kind = getattr(ev.error, "type", "")
        recoverable = getattr(ev.error, "recoverable", True)
        logger.warning("Session error type=%s recoverable=%s", kind, recoverable)
        if kind == "llm_error" and not recoverable and not ending:
            with contextlib.suppress(Exception):
                voice_agent.speak(flow.REPEAT_MESSAGE)

    def on_close(ev) -> None:
        logger.info("Session closed reason=%s", getattr(ev.reason, "value", ev.reason))
        if not ending and str(getattr(ev.reason, "value", "")) == "error":
            # The session can no longer speak; end the call rather than hold silence.
            with contextlib.suppress(Exception):
                spawn(ctx.delete_room())

    session.on("error", on_error)
    session.on("close", on_close)

    try:
        await session.start(
            agent=voice_agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                # BVCTelephony is tuned for 8 kHz narrowband phone audio.
                noise_cancellation=(
                    noise_cancellation.BVCTelephony() if telephony else noise_cancellation.BVC()
                ),
            ),
        )
    except Exception:
        logger.exception("Failed to start agent session")
        loading.cancel()
        raise
    logger.info("Audio session started after %.2fs", monotonic() - started)

    # ── Greeting: the published one if knowledge loads quickly, else a generic one ──
    knowledge: Any = None
    with contextlib.suppress(asyncio.TimeoutError):
        knowledge = await asyncio.wait_for(asyncio.shield(loading), flow.GREETING_WAIT_SECONDS)
    greeted = knowledge is None
    if greeted:
        voice_agent.speak(flow.GENERIC_GREETING)
        try:
            knowledge = await loading
        except Exception:
            logger.exception("Knowledge load failed")
            knowledge = None
    voice_agent.clinic_knowledge = knowledge
    finish = getattr(knowledge, "finish_call", None)
    if finish is not None:
        ctx.add_shutdown_callback(finish)
    snapshot = getattr(knowledge, "snapshot", None)
    logger.info("Knowledge %s after %.2fs", "loaded" if snapshot else "unavailable",
                monotonic() - started)
    if snapshot is None:
        await hang_up(flow.FAILURE_MESSAGES[flow.failure_for(getattr(knowledge, "reason", None))])
        return

    workspace = getattr(snapshot, "workspace", None)  # release; legacy keeps clinic fields
    supported = set(getattr(workspace or snapshot, "supported_languages", ()) or ())
    default_language = getattr(workspace or snapshot, "default_language", "")
    languages.allowed = (supported & flow.SARVAM_TTS_LANGUAGES) or set(flow.SARVAM_TTS_LANGUAGES)
    await voice_agent.update_instructions(knowledge.instructions)
    await voice_agent.update_tools(knowledge.function_tools())
    if not greeted:
        if voice_agent.tts is not None and default_language in flow.SARVAM_TTS_LANGUAGES:
            voice_agent.tts.update_options(target_language_code=default_language)
        greeting = getattr(knowledge, "greeting", None)
        if greeting:  # a release carries the agent's published greeting, said word for word
            voice_agent.session.say(greeting)
        else:
            voice_agent.session.generate_reply(
                instructions=getattr(knowledge, "greeting_instruction", None) or (
                    "Greet the caller warmly in one short sentence and ask how you can help. "
                    "Use Hindi unless the caller speaks English."
                )
            )

    # ── Maximum call duration ──────────────────────────────────────────
    async def time_limit() -> None:
        await asyncio.sleep(max(0.0, flow.max_call_seconds() - (monotonic() - started)))
        logger.info("Maximum call duration reached")
        await hang_up(flow.TIME_LIMIT_MESSAGE)

    spawn(time_limit())


# ════════════════════════════════════════════════════════════════════
#  ENTRY
# ════════════════════════════════════════════════════════════════════

def main():
    """Run the LiveKit worker. `src/agent.py` calls this for the deploy path."""
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            initialize_process_timeout=60,
            # Required for the SIP dispatch rule to target this worker.
            # Without it the worker auto-joins every room in the project.
            agent_name="inbound-agent",
        ),
    )


if __name__ == "__main__":
    main()
