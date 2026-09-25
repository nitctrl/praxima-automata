"""Privacy-safe voice diagnostics: never stringify errors or provider payloads."""

import json
from typing import Any, Literal

Stage = Literal["session", "stt", "tts", "playout", "turn", "hangup", "startup", "usage"]


def recoverable(event: Any) -> bool:
    # Provider events contain the exception; session events wrap a provider event.
    detail = event if hasattr(event, "recoverable") else getattr(event, "error", event)
    return getattr(detail, "recoverable", False) is True


def report(stage: Stage, event: Any) -> None:
    detail = event if hasattr(event, "recoverable") else getattr(event, "error", event)
    error = getattr(detail, "error", detail)
    status = getattr(error, "status_code", getattr(error, "status", None))
    # Use fixed categories, not provider messages, labels, URLs, or dynamic class names.
    category = "provider"
    if isinstance(error, TimeoutError):
        category = "timeout"
    elif isinstance(error, (ConnectionError, OSError)):
        category = "connection"
    elif isinstance(error, (TypeError, ValueError, RuntimeError)):
        category = "runtime"
    known_types = {
        "TimeoutError", "CancelledError", "RuntimeError", "TypeError", "ValueError",
        "APIConnectionError", "APIStatusError", "APITimeoutError", "TwirpError",
        "ClientConnectorError", "ClientConnectorDNSError", "ClientConnectorCertificateError",
        "WSServerHandshakeError", "ClientConnectionError", "ServerDisconnectedError",
        "UndefinedFunction", "InsufficientPrivilege", "CheckViolation", "QueueFull",
    }
    error_type = type(error).__name__
    cause = getattr(error, "__cause__", None)
    cause_type = type(cause).__name__
    print("Clinic voice diagnostic: " + json.dumps({
        "stage": stage, "category": category,
        "error_type": error_type if error_type in known_types else "other",
        "cause_type": cause_type if cause_type in known_types else None,
        "status": status if type(status) is int and 100 <= status <= 599 else None,
        "recoverable": recoverable(event),
    }), flush=True)