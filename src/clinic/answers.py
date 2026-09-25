"""Grounded answer generation for the dashboard's agent test."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from google import genai
from google.genai import types

from clinic.prompt import render_prompt
from clinic.snapshot import Snapshot

GroundedAnswerer = Callable[[str, Snapshot, Sequence[dict[str, Any]]], Awaitable[str]]


def gemini_answerer(api_key: str, model: str) -> GroundedAnswerer:
    """Create a short-answer generator that can only use retrieved passages."""

    async def answer(
        question: str, snapshot: Snapshot, passages: Sequence[dict[str, Any]]
    ) -> str:
        context = "\n\n".join(
            f"Source: {row.get('source', 'published document')}\n"
            f"Heading: {row.get('heading', '')}\n{row.get('text', '')}"
            for row in passages
        )
        system = render_prompt(snapshot) + (
            "\n\nThis is a dashboard test, so the relevant passages are already supplied. "
            "Do not call a tool; answer from those supplied passages."
        )
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=20_000, retry_options=types.HttpRetryOptions(attempts=2)
            ),
        )
        async with client.aio as aclient:
            response = await aclient.models.generate_content(
                model=model,
                contents=f"Question: {question}\n\nRetrieved passages:\n{context}",
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0,
                    max_output_tokens=220,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
        generated = (response.text or "").strip()
        if not generated:
            raise RuntimeError("Answer provider returned no text")
        return generated

    return answer
