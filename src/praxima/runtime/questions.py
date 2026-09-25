"""Bounded administrative routing over published facts, not a general language model."""

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from praxima.modules.catalog.domain.knowledge import Query, Result, StructuredKnowledge, match
from praxima.modules.releases.domain.snapshot import Doctor, normalize
from praxima.runtime.policy.safety import classify, response

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
NAME_END = (
    r"(?=\s+(?:be|is|available|availabe|availability|on|today|tomorrow|at|in|for|"
    r"fee|fees|hours|when|where|consultation|s|कब|कहाँ|कहां|की|का|हैं|है)(?=\s|$)|$)"
)


def question_language(text: str, current: str) -> str:
    value = normalize(text)
    if re.search(r"\b(?:in english|speak english)\b", value):
        return "en-IN"
    if re.search(r"[\u0900-\u097f]|\b(?:kahan|kab|kitne|kitna|baje|chahiye)\b", value):
        return "hi-IN"
    if re.search(r"\b(?:where|when|what|which|how|is|are|can|please)\b", value):
        return "en-IN"
    return current  # Names, numbers and affirmations must not change readback language.


@dataclass(frozen=True)
class Answer:
    text: str
    language: str
    action: str
    query: Query
    result: Result
    route: str = "administrative"

    def public(self) -> dict[str, Any]:
        return {
            "is_test": True, "answer": self.text, "language": self.language,
            "action": self.action, "query": self.query.model_dump(mode="json"),
            "result": self.result.model_dump(mode="json"), "route": self.route,
        }


def _doctor_name(value: str, doctors: list[Doctor]) -> str:
    explicit = re.search(r"(?:\bdr\b|\bdoctor\b|डॉक्टर)\s+(.+?)" + NAME_END, value)
    if explicit and explicit[1] not in {"available", "availability", "availabe"}:
        return explicit[1].strip()
    # Keep the complete supplied name; never reduce Rohan Sharma to Sharma.
    subject = re.search(r"\b(?:when|where) (?:is|would|will) (.+?)" + NAME_END, value)
    if subject and not re.search(r"\b(?:clinic|branch|location)\b", subject[1]):
        return subject[1]
    candidates = []
    for doctor in doctors:
        for name in (doctor.display_name, *doctor.aliases):
            key = normalize(name)
            if key and (value == key or value.startswith(key + " ")):
                candidates.append(key)
    return max(candidates, key=len) if candidates else ""


def _date(knowledge: StructuredKnowledge, text: str, value: str) -> str:
    unsupported = (
        r"\b(?:kal|yesterday|tonight|later|weekend|week|weeks|month|months|days)\b|कल|"
        r"\bday (?:after|before)\b|अगले|अगली|परसों|"
        r"\b(?:january|february|march|april|may|june|july|august|september|"
        r"october|november|december)\b"
    )
    if re.search(unsupported, value):
        raise ValueError("date")
    words = re.findall(r"\b(?:today|tomorrow|aaj)\b|आज", value)
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    weekdays = [day for day in WEEKDAYS if re.search(rf"\b{day}\b", value)]
    if len(set(words)) + len(dates) + len(weekdays) > 1:
        raise ValueError("date")
    if dates:
        day = dates[0]
    elif weekdays:
        delta = (WEEKDAYS.index(weekdays[0]) - knowledge.now().weekday()) % 7
        if re.search(r"\bnext\b", value):  # Locale-dependent: ask, do not assume.
            raise ValueError("date")
        day = (knowledge.now().date() + timedelta(days=delta)).isoformat()
    elif words:
        day = "tomorrow" if words[0] == "tomorrow" else "today"
    elif re.search(r"\bon\s+\d|\d[/.-]\d", text):
        raise ValueError("date")
    else:
        day = "today"
    return knowledge.day(day).isoformat()


def _time(value: str) -> tuple[Literal["any", "morning", "afternoon", "evening"], str, str]:
    bands: list[Literal["any", "morning", "afternoon", "evening"]] = [
        band for band in ("morning", "afternoon", "evening") if band in value.split()
    ]
    if len(bands) > 1:
        raise ValueError("time")
    bounds = {"after": "", "before": ""}
    for kind in bounds:
        found = re.search(rf"\b{kind} (\d{{1,2}})(?: (\d{{2}}))? (am|pm)\b", value)
        if found:
            hour, minute = int(found[1]), int(found[2] or 0)
            if not 1 <= hour <= 12 or minute > 59:
                raise ValueError("time")
            bounds[kind] = f"{hour % 12 + (12 if found[3] == 'pm' else 0):02d}:{minute:02d}"
        elif re.search(rf"\b{kind}\b", value):
            raise ValueError("time")
    if bounds["after"] and bounds["before"] and bounds["after"] >= bounds["before"]:
        raise ValueError("time")
    remaining = re.sub(r"\b(?:after|before) \d{1,2}(?: \d{2})? (?:am|pm)\b", "", value)
    if re.search(r"\b\d{1,2} (?:am|pm)\b|\bbaje\b|बजे|\b(?:noon|night)\b", remaining):
        raise ValueError("time")
    return bands[0] if bands else "any", bounds["after"], bounds["before"]


def answer_question(
    knowledge: StructuredKnowledge, text: str, *, language: str | None = None,
    previous: Answer | None = None,
) -> Answer:
    snapshot = knowledge.snapshot
    chosen = question_language(text, language or snapshot.default_language)
    if chosen not in snapshot.supported_languages:
        chosen = snapshot.default_language

    def say(english: str, hindi: str) -> str:
        return hindi if chosen == "hi-IN" else english

    def finish(message: str, result: Result, action: str = "clarify",
               query: Query | None = None, route: str = "administrative") -> Answer:
        return Answer(message, chosen, action, query or Query(), result, route)

    decision = classify(text)
    if len(text) > 500 or decision.route in {"medical", "emergency", "injection"}:
        return finish(
            response(decision, snapshot.emergency_message, chosen),
            Result(status="forbidden"), route=decision.route,
        )
    value = normalize(text)
    faq = knowledge.faq(text)
    if faq.status == "success":
        return finish(faq.data["answer"], faq, "faq")
    try:
        day = _date(knowledge, text, value)
    except ValueError:
        return finish(
            say("Which date do you mean? Please give a date in year-month-day form.",
                "कौन सी तारीख चाहिए? कृपया वर्ष-महीना-दिन में तारीख बताएं।"),
            Result(status="ambiguous", next_action="ask_for_date"),
        )
    doctors = [d for d in snapshot.doctors if d.effective(knowledge.day(day))]
    doctor = _doctor_name(value, doctors)
    fee = bool(re.search(r"\b(?:fees?|cost|price|charge|kitna|kitne)\b|फीस|शुल्क", value))
    availability = bool(re.search(
        r"\b(?:available|availabe|availability|when|where|schedule|timings?|hours|"
        r"open|close|kab|kahan)\b|कब|कहाँ|कहां|उपलब्ध|समय", value,
    ))
    has_doctor = bool(doctor or re.search(r"\b(?:dr|doctor)\b|डॉक्टर", value))
    if fee:
        action = "fees"
    elif has_doctor and availability:
        action = "availability"
    elif re.search(r"\b(?:where|address|location|directions|parking)\b|पता|कहाँ|कहां", value):
        action = "location"
    elif has_doctor or "doctors" in value.split():
        action = "doctors"
    elif re.search(r"\b(?:hours|open|close|timings?)\b|समय|खुल", value):
        action = "hours"
    else:
        action = "faq"
    query = Query(doctor=doctor, requested_date=day)
    is_follow_up = False
    if previous and previous.result.next_action == "ask_for_doctor":
        candidates = match(doctors, previous.query.doctor)
        selection = match(candidates, text.strip())
        if len(selection) == 1:
            action, is_follow_up = previous.action, True
            query = previous.query.model_copy(update={"doctor": str(selection[0].id)})
    if action == "availability" and not is_follow_up:
        try:
            band, after, before = _time(value)
        except ValueError:
            return finish(
                say("Please specify morning, afternoon, evening, or a time with AM or PM.",
                    "कृपया सुबह, दोपहर, शाम या AM/PM के साथ समय स्पष्ट करें।"),
                Result(status="ambiguous", next_action="ask_for_time"),
            )
        query = query.model_copy(update={
            "time_preference": band, "after": after, "before": before,
        })

    # Never switch tenants or silently drop an explicit branch supplied by a caller.
    named_place = re.search(
        r"\b(?:at|in) (.+?)(?=\s+(?:today|tomorrow|on|after|before)\b|$)", value,
    )
    if not named_place:
        named_place = re.search(r"\bwhere is (.+)$", value) if action == "location" else None
    if named_place:
        place = named_place[1].removesuffix(" located").strip()
        if place not in {"english", "hindi", "the clinic", "this clinic", "your clinic", "clinic",
                         normalize(snapshot.name)}:
            locations = match(snapshot.locations, place)
            if len(locations) != 1:
                return finish(
                    say("That location is not uniquely listed for this clinic. "
                        "Please specify a published branch.",
                        "यह स्थान स्पष्ट रूप से दर्ज नहीं है। प्रकाशित शाखा बताएं।"),
                    Result(status="not_found"), action, query,
                )
            query = query.model_copy(update={"location": str(locations[0].id)})
    services = [s for s in snapshot.services if s.effective(knowledge.day(day))]
    named_services = [s for s in services if f" {normalize(s.name)} " in f" {value} "]
    if len(named_services) == 1:
        query = query.model_copy(update={"service": str(named_services[0].id)})
    if action == "availability" or action == "fees" and (query.doctor or not query.service):
        matches = match(doctors, query.doctor)
        if not query.doctor or len(matches) != 1:
            if query.doctor and not matches:
                return finish(
                    say("That doctor is not in this clinic's published information. "
                        "Please check the full name.",
                        "यह डॉक्टर प्रकाशित जानकारी में नहीं मिला। कृपया पूरा नाम जांचें।"),
                    Result(status="not_found"), action, query,
                )
            names = "; ".join(d.display_name for d in matches)
            return finish(
                say(f"Which doctor do you mean: {names}? Please give the full name.",
                    f"कौन से डॉक्टर: {names}? कृपया पूरा नाम बताएं।"),
                Result(status="ambiguous", next_action="ask_for_doctor", data={
                    "matches": [{"reference": str(d.id), "name": d.display_name} for d in matches],
                }), action, query,
            )
        query = query.model_copy(update={"doctor": str(matches[0].id)})
    try:
        if action == "availability":
            result = knowledge.availability(query)
        elif action == "fees":
            result = knowledge.fee(query)
        elif action == "location":
            result = knowledge.location(query)
        elif action == "doctors":
            result = knowledge.find_doctors(Query(name=doctor, requested_date=day))
        elif action == "hours" and day == knowledge.now().date().isoformat():
            result = knowledge.current_status(query.location)
        else:
            result = Result(status="unavailable")
    except ValueError:
        result = Result(status="ambiguous", next_action="ask_for_date_or_time")
    data = result.data
    if result.status == "success" and action == "availability":
        name, place = data["doctor"]["name"], data["location"]["name"]
        separator = " से " if chosen == "hi-IN" else " to "
        windows = ", ".join(
            h["start"][11:16] + separator + h["end"][11:16] for h in data["hours"]
        )
        if windows:
            message = say(
                f"For {data['date']}, {name} has published hours at {place}: "
                f"{windows} ({data['timezone']}). "
                "These are working hours, not a confirmed appointment.",
                f"{data['date']} को {name}, {place} में: {windows} ({data['timezone']})। "
                "यह समय है, पक्की अपॉइंटमेंट नहीं।",
            )
        else:
            message = say(
                f"For {data['date']}, there are no remaining published hours for {name} "
                f"at {place} in the requested time window. "
                "This does not establish their next available date.",
                f"{data['date']} को {name} के लिए {place} में मांगे गए समय में "
                "कोई शेष प्रकाशित समय नहीं है। अगली उपलब्ध तारीख की पुष्टि नहीं है।",
            )
        if data.get("walk_in_restricted_hours"):
            message += say(" Walk-in restrictions apply during some listed hours.",
                           " कुछ समय में बिना अपॉइंटमेंट आने पर रोक है।")
        message += " " + " ".join(data.get("notices", []))
    elif result.status == "success" and action == "fees":
        message = say(
            f"Published fee: {data['amount']} {data['currency']} for {data['date']}.",
            f"प्रकाशित फीस: {data['amount']} {data['currency']}, तारीख {data['date']}।",
        )
    elif result.status == "success" and action == "location":
        message = f"{data['name']}: {data['address']}. {data.get('directions') or ''}"
        if re.search(r"\bparking\b", value):
            message += " " + (data.get("parking_information") or say(
                "Parking information is not published.", "पार्किंग जानकारी प्रकाशित नहीं है।",
            ))
    elif result.status in {"success", "ambiguous"} and action == "doctors":
        names = "; ".join(d["name"] for d in data.get("doctors", []))
        message = say(f"Published doctors: {names}.", f"प्रकाशित डॉक्टर: {names}।")
    elif result.status == "success" and action == "hours":
        times = ", ".join(f"{h['start'][11:16]}–{h['end'][11:16]}" for h in data["hours"])
        message = say(
            f"The clinic is {data['status']} now. "
            + (f"Published hours today: {times}." if times else "No hours are published today."),
            ("क्लिनिक अभी खुला है। " if data["status"] == "open" else "क्लिनिक अभी बंद है। ")
            + (f"आज का प्रकाशित समय: {times}।" if times else "आज का समय प्रकाशित नहीं है।"),
        )
        if data.get("walk_ins_restricted"):
            message += say(" Walk-ins are restricted now.", " अभी बिना अपॉइंटमेंट आने पर रोक है।")
        message += " " + " ".join(data.get("notices", []))
    elif result.status == "ambiguous":
        message = say("Please specify the service or location; more than one may match.",
                      "कृपया सेवा या स्थान स्पष्ट करें; एक से अधिक विकल्प हो सकते हैं।")
    else:
        message = say(
            "That information is not in this clinic's published configuration. "
            "Reception can help; I cannot assume hours or availability.",
            "यह जानकारी क्लिनिक के प्रकाशित विवरण में नहीं है। "
            "रिसेप्शन से पुष्टि करें; मैं समय या उपलब्धता का अनुमान नहीं लगा सकता।",
        )
    return finish(message.strip(), result, action, query)
