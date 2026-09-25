"""Bounded SDK metrics ingestion. No transcripts, prompts or provider error bodies."""

import asyncio
import json
import math
from uuid import UUID, uuid5

from livekit.agents.metrics import AgentMetrics, LLMMetrics, STTMetrics, TTSMetrics

from praxima.modules.engagement.application.sessions import CallOrchestrator
from praxima.runtime.voice_errors import report


def usage_event(session: UUID, metric: AgentMetrics) -> tuple[UUID, int, int, float, int] | None:
    if not isinstance(metric, (LLMMetrics, STTMetrics, TTSMetrics)):
        return None
    input_tokens = metric.prompt_tokens if isinstance(metric, LLMMetrics) else 0
    output_tokens = metric.completion_tokens if isinstance(metric, LLMMetrics) else 0
    seconds = metric.audio_duration if isinstance(metric, STTMetrics) else 0.0
    chars = metric.characters_count if isinstance(metric, TTSMetrics) else 0
    values = (input_tokens, output_tokens, seconds, chars)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Invalid provider usage units")
    identity = json.dumps(
        [
            metric.type,
            metric.label,
            metric.request_id,
            metric.timestamp,
            getattr(metric, "segment_id", None),
            values,
        ]
    )
    return uuid5(session, identity), input_tokens, output_tokens, seconds, chars


class UsageCollector:
    def __init__(self, call: CallOrchestrator) -> None:
        self.call = call
        self.queue: asyncio.Queue[tuple[UUID, int, int, float, int]] = asyncio.Queue(maxsize=128)
        self.worker = asyncio.create_task(self._consume(), name="clinic-usage")

    def submit(self, metric: AgentMetrics) -> None:
        try:
            event = usage_event(self.call.context.session_id, metric)
            if event:
                self.queue.put_nowait(event)
        except (ValueError, asyncio.QueueFull) as exc:
            report("usage", exc)
            self.call.stop_event.set()

    async def _consume(self) -> None:
        while True:
            event_id, inputs, outputs, seconds, chars = await self.queue.get()
            try:
                await self.call.service.usage(
                    self.call.context,
                    event_id,
                    input_tokens=inputs,
                    output_tokens=outputs,
                    stt_seconds=seconds,
                    tts_characters=chars,
                )
            except Exception as exc:
                report("usage", exc)
                self.call.stop_event.set()
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.queue.join(), 8)
        except asyncio.TimeoutError:
            self.call.stop_event.set()
        finally:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
