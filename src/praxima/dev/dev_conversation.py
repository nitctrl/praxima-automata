"""Deterministic development-only conversation; no model prose is ever spoken.

This intentionally small test path exercises voice/session/confirmation plumbing.
It does not pretend to be a production natural-language receptionist.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal
from uuid import UUID

from praxima.modules.catalog.domain.knowledge import StructuredKnowledge
from praxima.modules.engagement.application.sessions import CallOrchestrator
from praxima.modules.engagement.domain.requests import RequestDetails
from praxima.modules.releases.domain.snapshot import normalize
from praxima.runtime.policy.safety import classify
from praxima.runtime.questions import Answer, answer_question, question_language


@dataclass(frozen=True)
class Reply:
    text: str = field(repr=False)
    revision: UUID | None = None


class DevelopmentConversation:
    def __init__(self, call: CallOrchestrator) -> None:
        self.call = call
        self.stage: Literal["idle", "doctor", "date", "name", "phone", "confirm"] = "idle"
        self.kind: Literal["appointment", "callback"] = "callback"
        self.name = ""
        self.doctor: UUID | None = None
        self.day: date | None = None
        self.previous_answer: Answer | None = None

    def text(self, english: str, hindi: str) -> str:
        return hindi if self.call.language == "hi-IN" else english

    def reset(self) -> None:
        self.stage, self.name, self.doctor, self.day = "idle", "", None, None
        self.previous_answer = None
        self.call.confirmation.clear()

    def greeting(self) -> Reply:
        return Reply(
            self.text(
                "This is a fictional clinic development test, not a medical service. "
                "Say hours, doctors, appointment request, or callback. Use fictional details only.",
                "यह काल्पनिक क्लिनिक का परीक्षण है, चिकित्सा सेवा नहीं। "
                "समय, डॉक्टर, अपॉइंटमेंट अनुरोध या वापस कॉल कहें। केवल काल्पनिक जानकारी दें।",
            )
        )

    def doctor_list(self) -> str:
        knowledge = StructuredKnowledge(self.call.snapshot)
        doctors = [d for d in self.call.snapshot.doctors if d.effective(knowledge.now().date())]
        return "; ".join(
            f"{index}: {doctor.display_name}" for index, doctor in enumerate(doctors, 1)
        )

    async def turn(self, text: str) -> Reply:
        self.call._live()
        if len(text) > 500:
            self.reset()
            return Reply(self.call.snapshot.fallback_message)
        if self.stage == "idle":
            language = question_language(text, self.call.language)
            if language in self.call.snapshot.supported_languages:
                self.call.set_language(language)
        decision = classify(text)
        if decision.route in {"medical", "emergency", "injection"}:
            self.reset()
            _, safe = await self.call.user_turn(text)
            return Reply(safe or self.call.snapshot.fallback_message)
        value = normalize(text)
        if value in {"english", "speak english", "hindi", "हिंदी", "हिन्दी"}:
            language = "en-IN" if "english" in value else "hi-IN"
            self.reset()  # A language switch invalidates any old-language readback.
            self.call.set_language(language)
            self.call.activity()
            return self.greeting()
        if value in {"cancel", "stop", "नहीं", "रद्द"}:
            self.reset()
            await self.call.user_turn("cancel")
            return Reply(self.text("Pending request discarded.", "अधूरा अनुरोध रद्द किया गया।"))

        # Every utterance advances/invalidate confirmation before any potential write.
        await self.call.user_turn(text)
        if self.stage == "confirm":
            try:
                self.call.confirmation.confirmed()
            except ValueError:
                self.reset()
                return Reply(
                    self.text(
                        "Details were not confirmed. Start a new request.",
                        "जानकारी की पुष्टि नहीं हुई। नया अनुरोध शुरू करें।",
                    )
                )
            await self.call.persist_confirmed()
            self.reset()
            return Reply(
                self.text(
                    "Your test request was saved. This is not a confirmed booking.",
                    "परीक्षण अनुरोध दर्ज हुआ। यह पक्की बुकिंग नहीं है।",
                )
            )
        try:
            if self.stage == "doctor":
                if not re.fullmatch(r"[1-9][0-9]?", text.strip()):
                    raise ValueError
                today = StructuredKnowledge(self.call.snapshot).now().date()
                doctors = [d for d in self.call.snapshot.doctors if d.effective(today)]
                self.doctor = doctors[int(value) - 1].id
                self.stage = "date"
                return Reply(
                    self.text(
                        "Say today, tomorrow, or a date in YYYY-MM-DD form.",
                        "आज, कल की जगह tomorrow, या YYYY-MM-DD में तारीख कहें।",
                    )
                )
            if self.stage == "date":
                self.day = StructuredKnowledge(self.call.snapshot).day(text.strip())
                self.stage = "name"
                return Reply(
                    self.text(
                        "What fictional name should be on the request?",
                        "अनुरोध में कौन सा काल्पनिक नाम लिखें?",
                    )
                )
            if self.stage == "name":
                # Full name validator; no symptoms, arbitrary prose or model-provided fields.
                self.name = RequestDetails(
                    kind="callback", name=text.strip(), phone="+12025550109"
                ).name
                self.stage = "phone"
                return Reply(
                    self.text(
                        "Provide a fictional number with plus and country code.",
                        "प्लस और देश कोड के साथ काल्पनिक संपर्क नंबर दें।",
                    )
                )
            if self.stage == "phone":
                phone = re.sub(r"[\s()-]", "", text).replace("plus", "+")
                details = RequestDetails(
                    kind=self.kind,
                    name=self.name,
                    phone=phone,
                    doctor_id=self.doctor,
                    preferred_date=self.day,
                )
                # Contextual form fields are authorized only AFTER strict typed validation;
                # this flag is not a tool/model/caller parameter.
                self.call.tools_allowed = True
                pending = self.call.prepare_request(details)
                self.stage = "confirm"
                self.name = ""
                return Reply(pending["readback"], UUID(pending["revision"]))
        except (ValueError, IndexError):
            return Reply(
                self.text(
                    "That field was not understood. Please repeat it, or say cancel.",
                    "यह जानकारी समझ नहीं आई। दोहराएँ, या रद्द कहें।",
                )
            )

        if value in {"callback", "call back", "वापस कॉल"}:
            self.previous_answer = None
            self.kind, self.stage = "callback", "name"
            return Reply(
                self.text(
                    "What fictional name should be on the callback request?",
                    "वापस कॉल के अनुरोध में कौन सा काल्पनिक नाम लिखें?",
                )
            )
        if value in {"appointment", "appointment request", "अपॉइंटमेंट", "अपॉइंटमेंट अनुरोध"}:
            self.previous_answer = None
            self.kind, self.stage = "appointment", "doctor"
            return Reply(
                self.text("Choose a doctor by number. ", "डॉक्टर का नंबर चुनें। ") + self.doctor_list()
            )
        answer = answer_question(
            StructuredKnowledge(self.call.snapshot), text,
            language=self.call.language, previous=self.previous_answer,
        )
        self.previous_answer = answer if answer.result.status == "ambiguous" else None
        self.call.set_language(answer.language)
        await self.record_answer(answer)
        return Reply(answer.text)

    async def record_answer(self, answer: Answer) -> None:
        """Optional trusted adapter hook; never records the user's question."""

    def played(self, reply: Reply, *, interrupted: bool) -> None:
        self.call.activity()
        if reply.revision:
            self.call.confirmation.readback_completed(
                reply.revision, reply.text, interrupted=interrupted
            )
