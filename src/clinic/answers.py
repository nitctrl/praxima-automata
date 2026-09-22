"""Grounded answer generation for the dashboard's agent test."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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
        local_now = datetime.now(ZoneInfo(snapshot.timezone)).isoformat(timespec="minutes")
        system = render_prompt(snapshot) + (
            "\n\nThis is a dashboard test, so the relevant passages are already supplied. "
            "Answer the exact question directly in plain language. Do not copy the whole passage. "
            "For a date or date range, determine the weekday and distinguish being open at a "
            "specific time from being continuously open throughout a range. If the passages do "
            "not contain enough information, say so clearly."
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
                        f"Current clinic-local time: {local_now}\n"
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
