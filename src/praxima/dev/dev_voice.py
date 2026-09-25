"""Deterministic fictional voice adapter; ingress is enforced by each entrypoint."""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any

from livekit import rtc
from livekit.agents import Agent, llm, stt, tts, vad
from livekit.agents.voice import ModelSettings

from praxima.dev.dev_conversation import DevelopmentConversation, Reply
from praxima.dev.fallback_audio import FALLBACK, frames
from praxima.runtime.speech.normalize import english_speech
from praxima.runtime.voice_errors import report


class DevelopmentVoiceAgent(Agent):
    def __init__(
        self,
        conversation: DevelopmentConversation,
        *,
        speech_to_text: stt.STT[Any],
        voices: Mapping[str, tts.TTS[Any]],
        detector: vad.VAD,
        fallback: dict[str, tuple[int, bytes]],
    ) -> None:
        super().__init__(
            instructions="Deterministic fictional development test. No LLM replies.",
            stt=speech_to_text,
            tts=voices[conversation.call.language],
            llm=None,
            vad=detector,
            allow_interruptions=True,
            min_endpointing_delay=0.45,
            max_endpointing_delay=1.2,
        )
        self.conversation = conversation
        self.voices = voices
        self.fallback = fallback
        self._serial = asyncio.Lock()
        self._frames = 0
        self._failed = False

    async def _speak(self, reply: Reply) -> None:
        self._frames, self._failed = 0, False
        try:
            speech = self.session.say(reply.text, add_to_chat_ctx=False, allow_interruptions=True)
            await asyncio.wait_for(speech.wait_for_playout(), 40)
            self.conversation.played(
                reply,
                interrupted=(
                    speech.interrupted
                    or self._failed
                    or self._frames == 0
                    or self.conversation.call.stop_event.is_set()
                ),
            )
        except Exception as exc:
            report("playout", exc)
            self.conversation.played(reply, interrupted=True)
            self.conversation.call.stop_event.set()

    def provider_failed(self) -> None:
        self._failed = True
        self.conversation.call.confirmation.clear()
        self.conversation.call.stop_event.set()

    async def on_enter(self) -> None:
        async with self._serial:
            await self._speak(self.conversation.greeting())

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        text = new_message.text_content or ""
        new_message.content.clear()  # No model/chat history receives raw caller fields.
        turn_ctx.items.clear()
        async with self._serial:
            try:
                reply = await asyncio.wait_for(self.conversation.turn(text), 10)
                await self._speak(reply)
            except Exception as exc:
                report("turn", exc)
                self.conversation.reset()
                self.conversation.call.stop_event.set()
        raise llm.StopResponse()

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterator[rtc.AudioFrame]:
        content = ""
        async for chunk in text:
            content += chunk
            if len(content) > 3000:
                self._failed = True
                raise ValueError("Development response exceeds speech limit")
        engine = self.voices[self.conversation.call.language]
        if self.conversation.call.language == "en-IN":
            content = english_speech(content)
        try:
            async with engine.stream() as stream:
                stream.push_text(content)
                stream.end_input()
                async for event in stream:
                    self._frames += 1
                    yield event.frame
        except Exception as exc:
            report("tts", exc)
            self._failed = True
            self.conversation.call.stop_event.set()
            raise RuntimeError("Development speech unavailable") from None

    async def play_fallback(self) -> None:
        language = self.conversation.call.language
        # Local frames bypass the failed TTS engine entirely.
        try:
            speech = self.session.say(
                FALLBACK[language],
                audio=frames(self.fallback[language]),
                add_to_chat_ctx=False,
                allow_interruptions=False,
            )
            await asyncio.wait_for(speech.wait_for_playout(), 20)
        except Exception:
            pass  # Closing media must not depend on recovery audio success.

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[Any], model_settings: ModelSettings
    ) -> None:
        # Even accidental generate_reply() cannot escape into model-generated speech.
        return None
