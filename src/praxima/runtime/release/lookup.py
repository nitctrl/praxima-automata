"""Answers the voice tools compute from a pinned release snapshot, in memory, per call.

Pure functions (no I/O, clock injected): find and describe directory entries, work out hours
for a date, list live updates and search published text. Results are compact dictionaries
the model reads; everything comes from published content only.
"""

import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from praxima.modules.knowledge.domain.documents import tokens
from praxima.modules.releases.domain.agent_snapshot import AgentSnapshot, EntityItem
from praxima.shared.kernel.slots import DayException, WeeklyHours

MAX_TEXT = 700
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _words(text: str) -> set[str]:
    return set(tokens(text))


def _matches(query: set[str], words: Iterable[str]) -> int:
    """Query words found in `words`, allowing plurals and inflections (shared 4+ letter stem)."""
    pool = set(words)
    return sum(
        1
        for q in query
        if q in pool
        or any(len(q) >= 4 and (w.startswith(q) or q.startswith(w)) and len(w) >= 4 for w in pool)
    )


def _entity_words(snapshot: AgentSnapshot, entity: EntityItem) -> set[str]:
    kind = next((t for t in snapshot.entity_types if t.key == entity.type), None)
    fields = kind.searchable_fields if kind else []
    values = [entity.name, *entity.aliases, entity.type.replace("_", " ")]
    values += [str(entity.attributes.get(f, "")) for f in fields if f != "name"]
    return _words(" ".join(values))


def _brief(entity: EntityItem) -> dict[str, Any]:
    return {"name": entity.name, "type": entity.type, **entity.attributes}


def find_entities(
    snapshot: AgentSnapshot, query: str, entity_type: str | None = None, limit: int = 5
) -> dict[str, Any]:
    """Directory entries matching the words (e.g. "cardiology", "Sharma", "ECG")."""
    words = _words(query)
    scored = []
    for entity in snapshot.entities:
        if entity_type and entity.type != entity_type:
            continue
        score = _matches(words, _entity_words(snapshot, entity)) if words else 1
        if score:
            scored.append((score, entity.name.casefold(), entity))
    scored.sort(key=lambda s: (-s[0], s[1]))
    found = [_brief(e) for _, _, e in scored[:limit]]
    return {"status": "success" if found else "not_found", "entities": found}


def resolve_entity(snapshot: AgentSnapshot, name: str) -> EntityItem | None:
    """The best single match for a spoken name, key or alias (None if nothing or a tie)."""
    wanted = name.strip().casefold()
    exact = [
        e
        for e in snapshot.entities
        if wanted in {e.key, e.name.casefold(), *(a.casefold() for a in e.aliases)}
    ]
    if len(exact) == 1:
        return exact[0]
    words = _words(name)
    if not words:
        return None
    scored = sorted(
        ((_matches(words, _words(" ".join([e.name, *e.aliases]))), e) for e in snapshot.entities),
        key=lambda s: -s[0],
    )
    if not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return scored[0][1]


def _names(snapshot: AgentSnapshot) -> dict[str, EntityItem]:
    return {e.id: e for e in snapshot.entities}


def _links(snapshot: AgentSnapshot, entity: EntityItem) -> list[dict[str, Any]]:
    """What an entry is linked to, with any details on the link (fees …)."""
    by_id = _names(snapshot)
    links = []
    for relation in snapshot.relations:
        if entity.id not in (relation.from_entity_id, relation.to_entity_id):
            continue
        other_id = (
            relation.to_entity_id
            if relation.from_entity_id == entity.id
            else relation.from_entity_id
        )
        other = by_id.get(other_id)
        if other:
            links.append(
                {
                    "relation": relation.relation_type,
                    "with": other.name,
                    "with_type": other.type,
                    **relation.attributes,
                }
            )
    return links


def get_entity(snapshot: AgentSnapshot, name: str) -> dict[str, Any]:
    """One entry in full: its details and what it's linked to (fees, locations …)."""
    entity = resolve_entity(snapshot, name)
    if entity is None:
        return {
            "status": "not_found",
            "hint": "Ask which one the caller means, or use find_entities.",
        }
    return {"status": "success", "entity": _brief(entity), "links": _links(snapshot, entity)}


def _clock(value: str | None) -> time | None:
    return time.fromisoformat(value) if value else None


def _occurs_on(rule: str, day: date) -> bool:
    start = datetime.combine(day - timedelta(days=400), time())
    occurrences = rrulestr(rule, dtstart=start).between(
        datetime.combine(day, time()), datetime.combine(day, time(23, 59)), inc=True
    )
    return bool(occurrences)


def _day_hours(snapshot: AgentSnapshot, entity_id: str, day: date) -> dict[str, Any]:
    exception = next(
        (
            x
            for x in snapshot.availability.exceptions
            if x.entity_id == entity_id and x.date == day.isoformat()
        ),
        None,
    )
    label = f"{WEEKDAYS[day.weekday()]} {day.isoformat()}"
    if exception is not None:
        hours = (
            [f"{exception.start_time}-{exception.end_time}"]
            if exception.is_available and exception.start_time and exception.end_time
            else []
        )
        return {
            "date": label,
            "available": exception.is_available,
            "hours": hours,
            "note": exception.public_message,
        }
    hours = sorted(
        f"{r.start_time}-{r.end_time}"
        for r in snapshot.availability.rules
        if r.entity_id == entity_id and r.start_time and r.end_time and _occurs_on(r.rrule, day)
    )
    return {"date": label, "available": bool(hours), "hours": hours, "note": None}


def get_availability(
    snapshot: AgentSnapshot, name: str, now: datetime, on: str | None = None
) -> dict[str, Any]:
    """Published hours for one entry on a date (default: the next 7 days), exceptions applied."""
    entity = resolve_entity(snapshot, name)
    if entity is None:
        return {
            "status": "not_found",
            "hint": "Ask which one the caller means, or use find_entities.",
        }
    today = now.astimezone(ZoneInfo(snapshot.workspace.timezone)).date()
    if on:
        try:
            days = [date.fromisoformat(on)]
        except ValueError:
            return {"status": "invalid_date", "hint": "Use YYYY-MM-DD in the business timezone."}
    else:
        days = [today + timedelta(days=i) for i in range(7)]
    has_hours = any(r.entity_id == entity.id for r in snapshot.availability.rules) or any(
        x.entity_id == entity.id for x in snapshot.availability.exceptions
    )
    if not has_hours:
        return {"status": "no_published_hours", "entity": entity.name}
    return {
        "status": "success",
        "entity": entity.name,
        "timezone": snapshot.workspace.timezone,
        "today": today.isoformat(),
        "days": [_day_hours(snapshot, entity.id, d) for d in days],
        "note": "Published working hours, not bookable slots. Staff confirm every booking request.",
    }


def _window(item: Any, now: datetime) -> str | None:
    starts = datetime.fromisoformat(item.starts_at) if item.starts_at else None
    ends = datetime.fromisoformat(item.ends_at) if item.ends_at else None
    if ends is not None and ends <= now:
        return None
    if starts is not None and starts > now:
        return "scheduled"
    return "current"


def get_announcements(snapshot: AgentSnapshot, now: datetime) -> dict[str, Any]:
    """Live updates in force now, and scheduled ones with their exact times."""
    by_id = _names(snapshot)
    updates = []
    for item in sorted(snapshot.announcements, key=lambda a: -a.priority):
        state = _window(item, now)
        if state is None:
            continue
        about = by_id.get(item.entity_id or "") or by_id.get(item.location_entity_id or "")
        updates.append(
            {
                "state": state,
                "kind": item.kind,
                "message": item.message,
                "about": about.name if about else None,
                "starts_at": item.starts_at,
                "ends_at": item.ends_at,
            }
        )
    return {"status": "success", "updates": updates}


def _trim(text: str, words: set[str]) -> str:
    if len(text) <= MAX_TEXT:
        return text
    lowered = text.casefold()
    hit = min((lowered.find(w) for w in words if lowered.find(w) >= 0), default=0)
    start = max(0, hit - MAX_TEXT // 3)
    return ("…" if start else "") + text[start : start + MAX_TEXT].strip() + "…"


MAX_DIRECTORY_PASSAGES = 2


def _plain(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_plain(v) for v in value)
    return str(value)


def describe_entity(snapshot: AgentSnapshot, entity: EntityItem) -> str:
    """One readable line for a directory entry: its details and what it's linked to."""
    kind = next((t.name for t in snapshot.entity_types if t.key == entity.type), entity.type)
    parts = [f"{kind}: {entity.name}"]
    if entity.aliases:
        parts.append("also called " + ", ".join(entity.aliases))
    parts += [
        f"{key.replace('_', ' ').capitalize()}: {_plain(value)}"
        for key, value in entity.attributes.items()
        if value not in (None, "", [])
    ]
    for link in _links(snapshot, entity):
        extras = {k: v for k, v in link.items() if k not in ("relation", "with", "with_type")}
        detail = " ".join(_plain(v) for v in extras.values())
        relation = link["relation"].replace("_", " ").capitalize()
        parts.append(f"{relation}: {link['with']}" + (f" ({detail})" if detail else ""))
    return " · ".join(parts)


def search_knowledge(
    snapshot: AgentSnapshot, question: str, now: datetime, limit: int = 4
) -> dict[str, Any]:
    """Published sections, approved answers, live updates and directory entries for a question.

    Directory entries are included (at most two), so a question about a fee or an address
    finds its answer even if it didn't go to the directory tools. Sections and answers tagged
    with an entry the question names rank higher.
    """
    words = _words(question)
    if not words:
        return {"status": "success", "passages": []}
    passages: list[tuple[float, dict[str, Any]]] = []
    directory: list[tuple[float, dict[str, Any]]] = []
    named: set[str] = set()
    for entity in snapshot.entities:
        head = _matches(words, _words(" ".join([entity.name, *entity.aliases])))
        body = _matches(
            words,
            _words(
                " ".join(
                    [
                        entity.type.replace("_", " "),
                        *(_plain(v) for v in entity.attributes.values()),
                    ]
                )
            ),
        )
        if head:
            named.add(entity.id)
        if head or body:
            directory.append(
                (
                    2 * head + body,
                    {
                        "source": "directory",
                        "heading": entity.name,
                        "text": _trim(describe_entity(snapshot, entity), words),
                    },
                )
            )
    directory.sort(key=lambda p: -p[0])
    passages += directory[:MAX_DIRECTORY_PASSAGES]
    for faq in snapshot.faqs:
        head = _matches(words, _words(" ".join([faq.question, *faq.phrasings])))
        body = _matches(words, _words(faq.answer))
        tagged = 1 if faq.entity_id in named else 0
        if head or body:
            passages.append(
                (
                    2 * head + body + 0.5 + tagged,
                    {"source": "approved answer", "heading": faq.question, "text": faq.answer},
                )
            )
    for section in snapshot.knowledge_sections:
        head = _matches(words, _words(" ".join([section.heading or "", *section.keywords])))
        body = _matches(words, _words(section.text)) + (1 if section.entity_id in named else 0)
        if head or body:
            passages.append(
                (
                    2 * head + body,
                    {
                        "source": section.document_title,
                        "heading": section.heading,
                        "text": _trim(section.text, words),
                    },
                )
            )
    for item in snapshot.announcements:
        state = _window(item, now)
        score = _matches(words, _words(item.message))
        if state and score:
            label = "Current live update" if state == "current" else "Scheduled live update"
            passages.append(
                (
                    score + 1,
                    {
                        "source": label,
                        "heading": f"{item.starts_at} to {item.ends_at}",
                        "text": item.message,
                    },
                )
            )
    passages.sort(key=lambda p: -p[0])
    return {"status": "success", "passages": [p for _, p in passages[:limit]]}


_SPACES = re.compile(r"\s+")


def live_update_lines(snapshot: AgentSnapshot, now: datetime) -> list[str]:
    """One line per current or scheduled live update, for the system prompt."""
    lines = []
    for update in get_announcements(snapshot, now)["updates"]:
        about = f" ({update['about']})" if update["about"] else ""
        text = _SPACES.sub(" ", update["message"]).strip()
        window = f"{update['starts_at']} to {update['ends_at']}"
        lines.append(f"{update['state'].title()}{about}: {text} [{window}]")
    return lines


E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")


def request_kinds(snapshot: AgentSnapshot, allowed: set[str] | None = None) -> list[dict[str, Any]]:
    """The request types the caller may leave, with the fields each one takes."""
    kinds = []
    for kind in snapshot.work_item_kinds:
        if allowed is not None and kind.key not in allowed:
            continue
        fields = {
            name: (
                {"one_of": spec["enum"]}
                if "enum" in spec
                else {"type": spec.get("type", "string"), "pattern": spec.get("pattern")}
            )
            for name, spec in kind.payload_schema.get("properties", {}).items()
        }
        kinds.append(
            {"kind": kind.key, "name": kind.name, "fields": fields, "about": kind.subject_types}
        )
    return kinds


def prepare_request(
    snapshot: AgentSnapshot,
    kind: str,
    details: dict[str, Any],
    about: str = "",
    callback_number: str = "",
    allowed: set[str] | None = None,
) -> dict[str, Any]:
    """Check a request before it's stored: known kind, valid fields, allowed subject, number.

    Returns {"status": "ok", "payload": …, "entity_id": …} or {"status": "invalid", …} with
    what to fix (field names only, never values).
    """
    from praxima.shared.validation import schema_errors

    spec = next((k for k in snapshot.work_item_kinds if k.key == kind), None)
    if spec is None or (allowed is not None and kind not in allowed):
        return {
            "status": "invalid",
            "problem": "unknown request type",
            "types": [k["kind"] for k in request_kinds(snapshot, allowed)],
        }
    payload = {k: v for k, v in details.items() if v not in (None, "")}
    errors = schema_errors(spec.payload_schema, payload, "details")
    if errors:
        return {"status": "invalid", "fix": {e.field: e.message for e in errors}}
    entity_id = None
    if about:
        entity = resolve_entity(snapshot, about)
        if entity is None or entity.type not in spec.subject_types:
            return {
                "status": "invalid",
                "fix": {"about": f"Must name one {' or '.join(spec.subject_types) or 'nothing'}."},
            }
        entity_id = entity.id
    if callback_number and not E164.fullmatch(callback_number):
        return {
            "status": "invalid",
            "fix": {"callback_number": "Confirm the number with its country code, e.g. +9198…"},
        }
    return {"status": "ok", "payload": payload, "entity_id": entity_id}


# ------------------------------------------------------------------ slot booking


def entity_hours(
    snapshot: AgentSnapshot, entity_id: str
) -> tuple[list[WeeklyHours], list[DayException]]:
    """The entry's published weekly hours and dated exceptions, for open-slot maths."""
    rules = [
        WeeklyHours(r.rrule, time.fromisoformat(r.start_time), time.fromisoformat(r.end_time))
        for r in snapshot.availability.rules
        if r.entity_id == entity_id and r.start_time and r.end_time
    ]
    exceptions = [
        DayException(
            date.fromisoformat(x.date),
            x.is_available,
            _clock(x.start_time),
            _clock(x.end_time),
        )
        for x in snapshot.availability.exceptions
        if x.entity_id == entity_id
    ]
    return rules, exceptions


def describe_slots(starts: list[datetime], limit: int = 12) -> list[dict[str, str]]:
    """Slot starts as the caller hears them: date, weekday and 24-hour time."""
    return [
        {"date": s.date().isoformat(), "day": WEEKDAYS[s.weekday()], "time": s.strftime("%H:%M")}
        for s in starts[:limit]
    ]
