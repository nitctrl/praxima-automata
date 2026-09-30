"""
Voice AI Agent — Final Working Version
=======================================
Stack: LiveKit Agents + Sarvam STT/TTS + Anthropic Haiku

Architecture (confirmed from introspection):
  Agent      → ALL config: instructions, stt, llm, tts, vad, tools,
               turn_detection, interruption settings
  AgentSession → bare runtime shell, no config params
"""

import logging
import os
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    JobContext,
    JobProcess,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import anthropic, noise_cancellation, sarvam, silero

from clinic import observability
from clinic.agent_knowledge import AgentKnowledge, load_agent_knowledge
from clinic.ingress import trusted_destination as clinic_ingress
from clinic.resolver import InboundDestination
from clinic.vectors import VectorSearch

# ── Turn detector: optional ─────────────────────────────────────────
try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
    TURN_DETECTOR_AVAILABLE = True
except ImportError:
    TURN_DETECTOR_AVAILABLE = False

load_dotenv()

logger = logging.getLogger("voice-agent")
logger.setLevel(logging.INFO)

# Sarvam TTS rejects a reply outright if it shares no script with its fixed
# target_language_code ("Text must contain at least one character from the
# allowed languages"). Track the caller's detected language per turn and
# retarget the TTS to match, instead of leaving it locked to one language.
SARVAM_TTS_LANGUAGES = {
    "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN",
    "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
}


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
        clinic_knowledge: AgentKnowledge | None = None,
    ) -> None:
        self.clinic_knowledge = clinic_knowledge or AgentKnowledge(None)
        super().__init__(
            instructions=self.clinic_knowledge.instructions,

            # ── Tools ──────────────────────────────────────────────
            tools=self.clinic_knowledge.function_tools(),

            # ── STT: Sarvam Saaras v3 ─────────────────────────────
            stt=sarvam.STT(
                model="saaras:v3",
                language=os.environ.get("SARVAM_STT_LANGUAGE", "hi-IN"),
                api_key=os.environ.get("SARVAM_API_KEY"),
            ),

            # ── LLM: Anthropic Sonnet ─────────────────────────────
            llm=anthropic.LLM(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
                api_key=os.environ.get("ANTHROPIC_API_KEY"),
                temperature=0,
            ),

            # ── TTS: Sarvam Bulbul v3 ─────────────────────────────
            tts=sarvam.TTS(
                model=os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3"),
                target_language_code=os.environ.get("SARVAM_TTS_LANGUAGE", "hi-IN"),
                speaker=os.environ.get("SARVAM_TTS_SPEAKER", "shubh"),
                api_key=os.environ.get("SARVAM_API_KEY"),
            ),

            # ── VAD & Turn Detection (preloaded from prewarm) ─────
            vad=preloaded_vad,
            turn_detection=turn_detector,

            # ── Interruption & Endpointing ─────────────────────────
            # PSTN adds codec + jitter latency vs WebRTC, so endpointing
            # is relaxed on telephony calls to avoid cutting callers off.
            allow_interruptions=True,
            min_endpointing_delay=0.45 if telephony else 0.21,
            max_endpointing_delay=1.2 if telephony else 0.75,
            min_consecutive_speech_delay=0.3,
        )

    async def on_enter(self):
        """Speak immediately, without a database lookup or an LLM round trip."""
        english = os.environ.get("SARVAM_TTS_LANGUAGE", "hi-IN") == "en-IN"
        # No clinic identity is disclosed until ingress and the snapshot are verified.
        self.session.say(
            "Hello! I'm the automated clinic receptionist. How can I help?" if english else
            "नमस्ते! मैं क्लिनिक का स्वचालित रिसेप्शनिस्ट हूँ। मैं आपकी क्या मदद कर सकता हूँ?"
        )


# ════════════════════════════════════════════════════════════════════
#  LIFECYCLE
# ════════════════════════════════════════════════════════════════════

def prewarm(proc: JobProcess):
    """Pre-load heavy models once per worker process."""
    logger.info("Pre-warming: loading Silero VAD")
    proc.userdata["vad"] = silero.VAD.load()
    logger.info("VAD loaded")
    if TURN_DETECTOR_AVAILABLE:
        try:
            # The detector itself needs a live JobContext; only warm its imports here.
            from huggingface_hub import hf_hub_download  # noqa: F401
        except Exception:
            logger.warning("Turn detector dependency prewarm failed")
    # Load the embedding model once per worker, not on the audio loop of each call.
    try:
        vectors = VectorSearch.from_environment()
        if vectors is not None:
            vectors.preload()
    except Exception:
        logger.warning("Optional vector dependency prewarm failed")


async def entrypoint(ctx: JobContext):
    started = monotonic()
    observability.install(json_format=os.environ.get("LOG_FORMAT") == "json")
    observability.bind(correlation_id=ctx.room.name)
    logger.info(f"New job → room={ctx.room.name}")

    # ── Connect to room FIRST ──────────────────────────────────────
    await ctx.connect()

    # ── Wait for the caller before greeting ────────────────────────
    # On a phone call the SIP participant is bridged after the agent
    # joins; greeting before that clips the first words.
    participant = await ctx.wait_for_participant()

    # Telephony arrives either as a native LiveKit SIP participant or via the
    # WebSocket bridge, which flags itself with a carrier-neutral attribute.
    telephony = (
        participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        or participant.attributes.get("telephony") == "true"
    )
    carrier = participant.attributes.get("telephony.provider", "sip")
    observability.bind(telephony=telephony)
    caller = participant.attributes.get("sip.phoneNumber", "")
    logger.info(
        f"Participant joined: identity={participant.identity} "
        f"telephony={telephony} carrier={carrier} "
        f"caller={'***' + caller[-4:] if len(caller) > 4 else 'unknown'}"
    )

    turn_detector = None
    if TURN_DETECTOR_AVAILABLE:
        try:
            turn_detector = MultilingualModel()
        except Exception:
            logger.warning("Turn detector failed; using VAD-only")

    # ── AgentSession is BARE — no config params ────────────────────
    session = AgentSession()

    # Tenant = clinic resolved in the database from LiveKit's trusted SIP destination
    # (called number + trunk). An unknown or inactive number gets no clinic facts or tools.
    # Console has no dialled number and uses the development console clinic only.
    authorized = not telephony
    destination: InboundDestination | None = None
    if telephony:
        try:
            destination = clinic_ingress(participant.kind, participant.attributes).destination
            authorized = True
        except LookupError as rejected:
            logger.warning("Clinic ingress rejected, answering without clinic tools: %s", rejected)
    # Start audio before the (up to 20-second) database load. No clinic facts or tools
    # are available during initialization; caller input remains connected for barge-in.
    voice_agent = VoiceAgent(
        preloaded_vad=ctx.proc.userdata["vad"],
        turn_detector=turn_detector,
        telephony=telephony,
    )
    await voice_agent.update_tools([])
    await voice_agent.update_instructions(
        "You are an automated clinic receptionist. The greeting has already been provided. "
        "Clinic information is still loading. If the caller asks a question before it is "
        "ready, briefly ask them to wait; never invent clinic facts or claim a booking. "
        "Match their language silently using its native script. Do not provide medical advice."
    )

    def match_tts_language(ev) -> None:
        if ev.is_final and ev.language in SARVAM_TTS_LANGUAGES and voice_agent.tts:
            voice_agent.tts.update_options(target_language_code=ev.language)

    session.on("user_input_transcribed", match_tts_language)

    try:
        await session.start(
            agent=voice_agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                # BVCTelephony is tuned for 8 kHz narrowband phone audio;
                # BVC is for wideband mic input.
                noise_cancellation=(
                    noise_cancellation.BVCTelephony()
                    if telephony
                    else noise_cancellation.BVC()
                ),
            ),
        )
        logger.info("Audio session started after %.2fs", monotonic() - started)
        clinic_knowledge = (
            await load_agent_knowledge(Path(__file__).resolve().parents[1], destination)
            if authorized else AgentKnowledge(None)
        )
        # Backend-derived caller ID; the model cannot set it.
        clinic_knowledge.caller_number = caller
        ctx.add_shutdown_callback(clinic_knowledge.aclose)
        voice_agent.clinic_knowledge = clinic_knowledge
        await voice_agent.update_instructions(clinic_knowledge.instructions)
        await voice_agent.update_tools(clinic_knowledge.function_tools())
        logger.info(
            "Clinic knowledge %s after %.2fs",
            "loaded" if clinic_knowledge.snapshot else "unavailable", monotonic() - started,
        )
    except Exception:
        logger.exception("Failed to start agent session")
        raise


# ════════════════════════════════════════════════════════════════════
#  ENTRY
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
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
