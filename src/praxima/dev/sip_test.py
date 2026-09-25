"""Explicitly authorized single-number fictional SIP pilot. Not general onboarding."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from livekit import rtc

from praxima.dev.activation import load_development
from praxima.dev.dev_conversation import DevelopmentConversation, Reply
from praxima.dev.development import fixture_id
from praxima.dev.fallback_audio import FALLBACK, load_audio
from praxima.modules.agents.application.resolver import (
    ClinicResolver,
    ClinicUnavailable,
    ConfigurationRepository,
    InboundDestination,
    PostgresResolutionRepository,
)
from praxima.modules.releases.domain.snapshot import Snapshot
from praxima.runtime.policy.safety import classify
from praxima.runtime.questions import Answer
from praxima.shared.db.pool import RuntimeDatabase
from praxima.shared.db.settings import DatabaseSettings

NUMBER = "+918031907629"
TRUNK = "ST_rHwsznYTdTdR"
RULE = "SDR_q8m3G3dKocBR"
WORKER = "clinic-sip-test-agent"
PROJECT = "uwxdixxmpgvqfixvzqww"
LIVEKIT_URL = "wss://receptionist-00-5v8lby5a.livekit.cloud"
CLINIC = fixture_id("A")


async def preflight(root: Path) -> None:
    clinic, _ = load_development(root, PROJECT)
    if clinic != CLINIC:
        raise ValueError("Wrong clinic")
    for language in FALLBACK:
        load_audio(root / ".clinic-dev-audio" / f"{language}.wav")
    runtime = dotenv_values(root / ".env.runtime")
    settings = DatabaseSettings.validate(runtime.get("DATABASE_URL") or "", PROJECT)
    database = RuntimeDatabase(settings)
    await database.open()
    try:
        scope = await ClinicResolver(PostgresResolutionRepository(database)).resolve(
            InboundDestination(NUMBER, TRUNK, "plivo")
        )
        if scope.clinic_id != CLINIC:
            raise ValueError("Wrong clinic route")
        snapshot = Snapshot.model_validate(await ConfigurationRepository(database, scope).load())
        if set(snapshot.supported_languages) - set(FALLBACK):
            raise ValueError("Unsupported voice language")
        async with database.connection(CLINIC) as conn:
            row = await (await conn.execute(
                "SELECT has_function_privilege(current_user,"
                "'clinic_private.note_call(uuid,text,text)','EXECUTE') AS ready"
            )).fetchone()
            if not row or not row["ready"]:
                raise ValueError("Outcome migration required")
    finally:
        await database.close()


@dataclass(frozen=True)
class SipIngress:
    destination: InboundDestination
    call_id: str


def ingress(kind: int, attributes: Mapping[str, str]) -> SipIngress:
    """Only LiveKit's built-in ingress attributes, never custom headers or caller ID."""
    if (
        kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        or attributes.get("sip.trunkPhoneNumber") != NUMBER
        or attributes.get("sip.trunkID") != TRUNK
        or attributes.get("sip.ruleID") != RULE
    ):
        raise ClinicUnavailable("SIP test ingress rejected")
    call_id = attributes.get("sip.callID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call_id):
        raise ClinicUnavailable("SIP call identifier unavailable")
    return SipIngress(InboundDestination(NUMBER, TRUNK, "plivo"), call_id)


class SipTestConversation(DevelopmentConversation):
    def greeting(self) -> Reply:
        return Reply(self.text(
            "You have reached an automated fictional clinic phone test, not a medical service. "
            "Ask about doctors, hours, or fees. Use fictional details only.",
            "यह स्वचालित काल्पनिक क्लिनिक का फोन परीक्षण है, चिकित्सा सेवा नहीं। "
            "डॉक्टर, समय या फीस पूछें। केवल काल्पनिक जानकारी दें।",
        ))

    async def record_answer(self, answer: Answer) -> None:
        await self.call.service.note(self.call.context, answer.action, answer.result.status)

    async def turn(self, text: str) -> Reply:
        reply = await super().turn(text)
        if classify(text).route in {"medical", "emergency", "injection"}:
            await self.call.service.note(self.call.context, "clarify", "forbidden")
        return reply