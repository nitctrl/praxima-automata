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

from clinic.agent_knowledge import AgentKnowledge, load_agent_knowledge
from clinic.sip_test import ingress as clinic_ingress

# ── Turn detector: optional ─────────────────────────────────────────
try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
    TURN_DETECTOR_AVAILABLE = True
except ImportError:
    TURN_DETECTOR_AVAILABLE = False

load_dotenv()

logger = logging.getLogger("voice-agent")
logger.setLevel(logging.INFO)


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
                temperature=0.7,
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
        """Agent speaks first when the caller is connected."""
        self.session.generate_reply(
            instructions=(
                "Greet the caller warmly in one short sentence and ask how "
                "you can help with this fictional test clinic. "
                "Use Hindi unless the caller speaks English."
            )
        )


# ════════════════════════════════════════════════════════════════════
#  LIFECYCLE
# ════════════════════════════════════════════════════════════════════

def prewarm(proc: JobProcess):
    """Pre-load heavy models once per worker process."""
    logger.info("Pre-warming: loading Silero VAD")
    proc.userdata["vad"] = silero.VAD.load()
    logger.info("VAD loaded")


async def entrypoint(ctx: JobContext):
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
    caller = participant.attributes.get("sip.phoneNumber", "")
    logger.info(
        f"Participant joined: identity={participant.identity} "
        f"telephony={telephony} carrier={carrier} "
        f"caller={'***' + caller[-4:] if len(caller) > 4 else 'unknown'}"
    )

    # ── Build turn detector (optional) ─────────────────────────────
    turn_detector = None
    if TURN_DETECTOR_AVAILABLE:
        try:
            turn_detector = MultilingualModel()
            logger.info("Multilingual turn detector active")
        except Exception as e:
            logger.warning(f"Turn detector failed, VAD-only: {e}")
    else:
        logger.info("No turn detector plugin — using VAD-only")

    # ── AgentSession is BARE — no config params ────────────────────
    session = AgentSession()

    # Knowledge-only integration: no clinic call orchestration, usage writes or custom speech.
    # Console uses the same single-clinic knowledge. Never expose it on another SIP route.
    authorized = not telephony
    if telephony:
        try:
            clinic_ingress(participant.kind, participant.attributes)
            authorized = True
        except LookupError:
            pass
    clinic_knowledge = (
        await load_agent_knowledge(Path(__file__).resolve().parents[1])
        if authorized else AgentKnowledge(None)
    )
    logger.info("Clinic knowledge %s", "loaded" if clinic_knowledge.snapshot else "unavailable")

    # ── Start with fully-configured Agent ──────────────────────────
    try:
        await session.start(
            agent=VoiceAgent(
                preloaded_vad=ctx.proc.userdata["vad"],
                turn_detector=turn_detector,
                telephony=telephony,
                clinic_knowledge=clinic_knowledge,
            ),
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
        logger.info("Agent session running")
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
            # Required for the SIP dispatch rule to target this worker.
            # Without it the worker auto-joins every room in the project.
            agent_name="inbound-agent",
        ),
    )
