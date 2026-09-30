"""Structured, redacted, call-correlated logs and stage-latency events.

Every record logged while a call is active carries ``clinic_id``, ``configuration_version_id``
and ``correlation_id`` (the LiveKit room/job), propagated through ``contextvars`` so tools,
repositories and vector search inherit them without plumbing. Phone numbers are masked at
the handler, so no module can leak one by accident. Transcripts, prompts, SQL and provider
payloads must never be logged; this layer is a backstop, not a licence.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("clinic.metrics")

CONTEXT_FIELDS = ("correlation_id", "clinic_id", "configuration_version_id", "telephony")
_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "clinic_log_context", default={}
)
# Phone-like digit runs (10-15 digits, optional +). Dates, UUIDs and short counts survive.
# "_" and ":" are separators, not word characters, so "sip_+9198..." is still masked.
PHONE = re.compile(r"(?<![0-9A-Za-z.])\+?\d{10,15}(?![0-9A-Za-z.])")


def bind(**fields: object) -> None:
    """Attach fields to every log record in the current call's task tree."""
    current = dict(_context.get())
    current.update({key: str(value) for key, value in fields.items() if value is not None})
    _context.set(current)


def context() -> dict[str, str]:
    return dict(_context.get())


def mask(text: str) -> str:
    return PHONE.sub(lambda m: "***" + re.sub(r"\D", "", m.group())[-4:], text)


class ContextFilter(logging.Filter):
    """Adds call context and masks phone numbers on every record a handler emits."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = mask(message)
        if masked != message:
            record.msg, record.args = masked, None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (*CONTEXT_FIELDS, "event", "stage", "duration_ms", "outcome"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            # Exception type only: provider messages may contain caller content.
            payload["exception"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False)


def install(*, json_format: bool = False) -> None:
    """Idempotently add the context/redaction filter (and optional JSON) to root handlers."""
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if not any(isinstance(item, ContextFilter) for item in handler.filters):
            handler.addFilter(ContextFilter())
        if json_format and not isinstance(handler.formatter, JsonFormatter):
            handler.setFormatter(JsonFormatter())


def event(name: str, *, level: int = logging.INFO, **fields: object) -> None:
    """One structured metric/audit event; fields must be non-PII scalars."""
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.log(level, "%s %s", name, details, extra={"event": name, **fields})


@contextmanager
def timed(stage: str) -> Iterator[None]:
    """Emit ``stage_latency`` with duration and outcome for one pipeline stage."""
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        event(
            "stage_latency",
            stage=stage,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            outcome=outcome,
        )
