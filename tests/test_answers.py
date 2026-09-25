import asyncio
from types import SimpleNamespace

from test_structured_knowledge import content  # noqa: F401

import praxima.integrations.llm.gemini as answers
from praxima.modules.releases.domain.snapshot import Snapshot


def test_grounded_answer_prompt(content, monkeypatch):  # noqa: F811
    captured = {}

    class Models:
        async def generate_content(self, **values):
            captured.update(values)
            return SimpleNamespace(text="The published text is insufficient.")

    class Aio:
        models = Models()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Client:
        def __init__(self, **values):
            captured["client"] = values
            self.aio = Aio()

    monkeypatch.setattr(answers.genai, "Client", Client)
    snapshot = Snapshot.model_validate(content)
    generate = answers.gemini_answerer("private-test-key", "test-model")
    response = asyncio.run(generate(
        "Will the clinic be open on September 17?",
        snapshot,
        [{"source": "clinic.md", "heading": "Timings", "text": "Doctor hours only."}],
    ))
    assert response == "The published text is insufficient."
    compact_system = " ".join(captured["config"].system_instruction.split())
    assert "specific date or date range" in compact_system
    assert "Doctor working hours do not prove" in compact_system
    assert "Doctor hours only" in captured["contents"]
    assert captured["client"]["api_key"] == "private-test-key"
