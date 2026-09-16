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
import json
from typing import Annotated
from pathlib import Path

from dotenv import load_dotenv

from livekit import rtc
from livekit.agents import (
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    RoomInputOptions,
    function_tool,
)
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import sarvam, anthropic, noise_cancellation, silero

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
#  RAG KNOWLEDGE BASE
# ════════════════════════════════════════════════════════════════════

class KnowledgeBase:
    """
    Pluggable RAG backend.
    Swap search() with vector DB (Pinecone/Qdrant/Weaviate) for production.
    """

    def __init__(self, documents: list[dict] | None = None):
        self.documents = documents or []

    @classmethod
    def from_json(cls, path: str) -> "KnowledgeBase":
        file = Path(path)
        if not file.exists():
            logger.warning(f"KB file not found: {path}")
            return cls([])
        with open(file) as f:
            return cls(json.load(f))

    @classmethod
    def default(cls) -> "KnowledgeBase":
        return cls([
            {
                "topic": "pricing",
                "content": (
                    "Starter plan is ₹2,499/month, Pro is ₹6,999/month, "
                    "Enterprise is custom. All include a 14-day free trial."
                ),
                "keywords": [
                    "price", "pricing", "cost", "plan", "subscription",
                    "pay", "money", "expensive", "cheap", "free trial",
                ],
            },
            {
                "topic": "business_hours",
                "content": (
                    "Open Monday–Friday 9 AM to 6 PM IST, "
                    "Saturday 10 AM–2 PM. Closed Sundays and public holidays."
                ),
                "keywords": [
                    "hours", "open", "close", "time", "schedule",
                    "available", "when", "today", "tomorrow",
                ],
            },
            {
                "topic": "refund_policy",
                "content": (
                    "30-day money-back guarantee, no questions asked. "
                    "Email support@example.com to request a refund."
                ),
                "keywords": [
                    "refund", "return", "money back", "cancel",
                    "cancellation", "guarantee",
                ],
            },
            {
                "topic": "contact",
                "content": (
                    "Email: support@example.com, Phone: +91-80-1234-5678, "
                    "or use live chat on our website."
                ),
                "keywords": [
                    "contact", "email", "phone", "call",
                    "reach", "support", "help", "chat",
                ],
            },
        ])

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        query_lower = query.lower()
        scored = []
        for doc in self.documents:
            score = sum(1 for kw in doc.get("keywords", []) if kw in query_lower)
            if score > 0:
                scored.append({
                    "topic": doc["topic"],
                    "content": doc["content"],
                    "score": score,
                })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]


knowledge_base = KnowledgeBase.default()


# ════════════════════════════════════════════════════════════════════
#  RAG TOOL
# ════════════════════════════════════════════════════════════════════

@function_tool()
async def lookup_info(
    query: Annotated[
        str,
        "The user's question or topic to search, e.g. 'pricing' or 'business hours'"
    ],
) -> str:
    """Search the business knowledge base for factual information about
    pricing, hours, policies, contact details, or services.
    Always call this before answering any business-specific question."""

    logger.info(f"[RAG] query={query!r}")
    results = knowledge_base.search(query, top_k=3)

    if not results:
        logger.info("[RAG] no results")
        return (
            "No relevant information found in the knowledge base. "
            "Let the user know you don't have that information "
            "and suggest they contact support."
        )

    context = "\n".join(f"• [{r['topic']}] {r['content']}" for r in results)
    logger.info(f"[RAG] {len(results)} results returned")
    return (
        f"Knowledge base results:\n{context}\n\n"
        "Use this to answer the user naturally and concisely."
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
        super().__init__(
            instructions="""
You are a helpful, friendly voice assistant for our business.

LANGUAGE RULES (CRITICAL):
- Detect the language the user is speaking and respond ONLY in that same language.
- If the user speaks Hindi, respond ENTIRELY in Hindi. Do NOT add English translations.
- If the user speaks English, respond ENTIRELY in English. Do NOT add Hindi translations.
- NEVER mix languages unless the user itself speaks in mixed language such as HINGLISH(HINDI+ENGLISH) in the same response. NEVER repeat yourself in a second language.
- If the user switches language mid-conversation, switch with them.

RESPONSE RULES:
- Keep every response to 1–3 short sentences. Voice ≠ text — be concise.
- Sound natural, warm, and conversational — like a real person on a call.
- Use short sentences. Break long thoughts into separate sentences.
- It's okay to start with brief acknowledgements like "Right," "Okay," or "Got it" before answering.
- NEVER output any markup, tags, angle brackets, or timing notations in your text.
  Your output goes directly to a text-to-speech engine that reads everything literally.
- When users ask about pricing, hours, refunds, or company info,
  ALWAYS call the lookup_info tool first. Never guess business facts.
- If the tool returns no results, say so honestly.
- Greet the user warmly when the conversation starts.

            """,

            # ── Tools ──────────────────────────────────────────────
            tools=[lookup_info],

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
                model="bulbul:v3",
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
                "you can help. Use Hindi unless the caller speaks English."
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

    # ── Start with fully-configured Agent ──────────────────────────
    try:
        await session.start(
            agent=VoiceAgent(
                preloaded_vad=ctx.proc.userdata["vad"],
                turn_detector=turn_detector,
                telephony=telephony,
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