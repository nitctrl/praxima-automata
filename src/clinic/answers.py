"""Grounded answer generation for the dashboard's agent test."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from anthropic import AsyncAnthropic

from clinic.prompt import render_prompt
from clinic.snapshot import Snapshot

GroundedAnswerer = Callable[[str, Snapshot, Sequence[dict[str, Any]]], Awaitable[str]]


def anthropic_answerer(api_key: str, model: str) -> GroundedAnswerer:
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
        async with AsyncAnthropic(api_key=api_key, timeout=20, max_retries=1) as client:
            response = await client.messages.create(
                model=model,
                max_tokens=220,
                temperature=0,
                system=system,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\nRetrieved passages:\n{context}"
                    ),
                }],
            )
        generated = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not generated:
            raise RuntimeError("Answer provider returned no text")
        return generated

    return answer
