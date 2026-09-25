import json
from types import SimpleNamespace

import pytest
from livekit.agents.stt import STTError
from livekit.agents.tts import TTSError
from livekit.agents.voice.events import ErrorEvent

from praxima.runtime.voice_errors import recoverable, report


@pytest.mark.parametrize("event_type", [STTError, TTSError])
@pytest.mark.parametrize("retryable", [True, False])
@pytest.mark.parametrize("wrapped", [True, False])
def test_sdk_error_shapes_and_private_payloads(event_type, retryable, wrapped, capsys):
    error = RuntimeError("secret key, caller transcript and private URL")
    event = event_type(timestamp=1.0, label="private provider label", error=error,
                       recoverable=retryable)
    if wrapped:
        event = ErrorEvent(error=event, source=object())
    assert recoverable(event) is retryable
    report("stt", event)
    output = capsys.readouterr().out
    assert "private" not in output and "secret" not in output and "transcript" not in output
    assert json.loads(output.split(": ", 1)[1])["recoverable"] is retryable


def test_unknown_errors_fail_closed_and_status_is_numeric(capsys):
    event = SimpleNamespace(error=SimpleNamespace(status_code="private response"))
    assert not recoverable(event)
    report("hangup", event)
    assert json.loads(capsys.readouterr().out.split(": ", 1)[1])["status"] is None