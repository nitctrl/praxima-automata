import asyncio
from types import SimpleNamespace

from test_structured_knowledge import content  # noqa: F401

import clinic.answers as answers
from clinic.snapshot import Snapshot


def test_grounded_answer_prompt(content, monkeypatch):  # noqa: F811
    captured = {}

    class Messages:
        async def create(self, **values):
            captured.update(values)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="The published text is insufficient.")]
            )

    class Client:
        def __init__(self, **values):
            captured["client"] = values
            self.messages = Messages()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(answers, "AsyncAnthropic", Client)
    snapshot = Snapshot.model_validate(content)
    generate = answers.anthropic_answerer("private-test-key", "test-model")
    response = asyncio.run(generate(
        "Will the clinic be open on September 17?",
        snapshot,
        [{"source": "clinic.md", "heading": "Timings", "text": "Doctor hours only."}],
    ))
    assert response == "The published text is insufficient."
    compact_system = " ".join(captured["system"].split())
    assert "specific date or date range" in compact_system
    assert "Doctor working hours do not prove" in compact_system
    assert "Doctor hours only" in captured["messages"][0]["content"]
    assert captured["client"]["api_key"] == "private-test-key"
