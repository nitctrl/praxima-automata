"""Conservative deterministic routing, not diagnosis or clinical triage.

Unrecognized utterances never receive a medical answer from this layer. This
is defense in depth, not comprehensive language understanding or certification.
"""

import re
from dataclasses import dataclass
from typing import Literal

from clinic.snapshot import normalize

Route = Literal["emergency", "medical", "injection", "administrative", "unknown"]


@dataclass(frozen=True)
class SafetyDecision:
    route: Route
    allow_tools: bool


def classify(text: str) -> SafetyDecision:
    value = normalize(text[:2000])
    patterns: list[tuple[Route, str]] = [
        (
            "emergency",
            r"\b(emergency|suicide|overdose|unconscious|can t breathe|cannot breathe|"
            r"chest pain|kill myself|bleeding heavily|saans nahi|behosh)\b|आपातकाल|बेहोश|सांस नहीं",
        ),
        (
            "medical",
            r"\b(medicine|medication|dose|dosage|diagnos\w*|symptom\w*|prescri\w*|"
            r"treatment|lab report|test result|pain|fever|tablet|dawai|dava|bukhar|dard)\b|"
            r"दवा|दवाई|खुराक|इलाज|रिपोर्ट|बुखार|दर्द",
        ),
        (
            "injection",
            r"\b(ignore\w*|system prompt|api key|password|credentials|other clinic|"
            r"another clinic|change the fee|change the schedule|disable\w*|secret\w*)\b|"
            r"पासवर्ड|दूसरे क्लिनिक|नियम भूल",
        ),
    ]
    for route, pattern in patterns:
        if re.search(pattern, value):
            return SafetyDecision(route, False)
    if re.search(
        r"\b(doctor|hours|open|close|fee|price|appointment|callback|reception|"
        r"location|address|parking|registration|tomorrow|today|sharma|hello|hi|"
        r"yes|no|correct|confirm|cancel|hindi|english|haan|nahi|kal|aaj)\b|"
        r"डॉक्टर|समय|फीस|पता|अपॉइंटमेंट|नमस्ते|हाँ|हां|नहीं|हिंदी",
        value,
    ):
        return SafetyDecision("administrative", True)
    return SafetyDecision("unknown", False)


def response(decision: SafetyDecision, emergency: str, language: str = "en-IN") -> str:
    if decision.route == "emergency":
        return emergency
    if language.startswith("hi"):
        if decision.route == "medical":
            return "मैं स्वचालित प्रशासनिक सहायक हूँ। चिकित्सा सलाह नहीं दे सकता। कृपया डॉक्टर से संपर्क करें।"
        return "मेरे पास इसकी पुष्टि की हुई जानकारी नहीं है। कृपया रिसेप्शन से संपर्क करें।"
    if decision.route == "medical":
        return (
            "I am an automated administrative assistant and cannot give medical advice. "
            "Please contact a clinician."
        )
    return "I do not have confirmed information for that request. Please contact reception."


def output_allowed(text: str) -> bool:
    """Supplemental claim filter; never authorize arbitrary model prose with this alone."""
    return not re.search(
        r"\b(take \d+|you have (?:a |the )?(?:disease|infection)|harmless|nothing to worry|"
        r"appointment (?:is )?confirmed|booking (?:is )?confirmed|guaranteed slot)\b|"
        r"अपॉइंटमेंट पक्का|दवा ले|घबराने की बात नहीं",
        text.casefold(),
    )
