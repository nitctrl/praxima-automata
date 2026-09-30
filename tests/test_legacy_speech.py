"""Protect the restored voice pipeline while adding only clinic knowledge."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents.voice import Agent


def test_original_voice_hooks_are_not_overridden():
    from agent import VoiceAgent

    assert VoiceAgent.tts_node is Agent.tts_node
    assert VoiceAgent.on_user_turn_completed is Agent.on_user_turn_completed
    assert VoiceAgent.llm_node is Agent.llm_node


def test_only_unified_rag_tool_is_attached(monkeypatch):
    import agent

    captured = {}
    monkeypatch.setattr(agent.Agent, "__init__", lambda self, **kw: captured.update(kw))
    for provider, name in [(agent.sarvam, "STT"), (agent.sarvam, "TTS"), (agent.anthropic, "LLM")]:
        monkeypatch.setattr(provider, name, Mock())
    agent.VoiceAgent(telephony=True)
    assert len(captured["tools"]) == 1
    assert not hasattr(agent, "lookup_info")
    assert "Clinic knowledge is unavailable" in captured["instructions"]
    assert captured["min_endpointing_delay"] == 0.45
    assert captured["max_endpointing_delay"] == 1.2
    assert agent.anthropic.LLM.call_args.kwargs["temperature"] == 0


def test_no_clinic_orchestration_or_custom_speech_imported_by_agent():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/agent.py").read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not modules.intersection({"clinic.dev_voice", "clinic.sessions", "clinic.usage"})


@pytest.mark.parametrize("language,expected", [("hi-IN", "नमस्ते!"), ("en-IN", "Hello!")])
def test_greeting_uses_direct_tts_without_llm(monkeypatch, language, expected):
    from agent import VoiceAgent

    monkeypatch.setenv("SARVAM_TTS_LANGUAGE", language)
    session = Mock()
    asyncio.run(VoiceAgent.on_enter(SimpleNamespace(session=session)))
    assert session.say.call_args.args[0].startswith(expected)
    session.generate_reply.assert_not_called()


@pytest.mark.parametrize("authorized", [True, False])
def test_audio_starts_before_knowledge_and_rejected_route_never_loads(monkeypatch, authorized):
    import agent

    events = []
    session = Mock()
    voice = SimpleNamespace(
        update_tools=AsyncMock(),
        update_instructions=AsyncMock(),
        session=session,
    )
    loaded = agent.AgentKnowledge(None)
    loaded.caller_number = ""

    async def start(**kwargs):
        assert voice.update_tools.call_args.args == ([],)
        events.append("audio")
        await agent.VoiceAgent.on_enter(voice)

    async def load(root, destination):
        session.say.assert_called_once()
        events.append("knowledge")
        # Yield as a slow database would; the greeting is already scheduled.
        await asyncio.sleep(0)
        return loaded

    participant = SimpleNamespace(
        kind=agent.rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        attributes={},
        identity="test-caller",
    )
    ctx = SimpleNamespace(
        room=SimpleNamespace(name="test-room"),
        proc=SimpleNamespace(userdata={"vad": Mock(), "turn_detector": Mock()}),
        connect=AsyncMock(),
        wait_for_participant=AsyncMock(return_value=participant),
        add_shutdown_callback=Mock(),
    )
    session.start = AsyncMock(side_effect=start)
    monkeypatch.setattr(agent, "AgentSession", Mock(return_value=session))
    monkeypatch.setattr(agent, "MultilingualModel", Mock())
    # Preserve on_enter for the fake session's startup callback.
    voice_type = agent.VoiceAgent
    factory = Mock(return_value=voice)
    factory.on_enter = voice_type.on_enter
    monkeypatch.setattr(agent, "VoiceAgent", factory)
    monkeypatch.setattr(agent, "load_agent_knowledge", AsyncMock(side_effect=load))
    monkeypatch.setattr(
        agent, "clinic_ingress", Mock(side_effect=None if authorized else LookupError("unknown"))
    )
    monkeypatch.setattr(agent.noise_cancellation, "BVCTelephony", Mock())
    asyncio.run(agent.entrypoint(ctx))
    assert events == (["audio", "knowledge"] if authorized else ["audio"])
    assert voice.clinic_knowledge.snapshot is None
    assert voice.update_instructions.call_args.args == (voice.clinic_knowledge.instructions,)
    ctx.add_shutdown_callback.assert_called_once_with(voice.clinic_knowledge.aclose)


def test_heavy_dependencies_are_preloaded(monkeypatch):
    import agent

    detector, vectors = Mock(), Mock()
    monkeypatch.setattr(agent, "TURN_DETECTOR_AVAILABLE", True)
    monkeypatch.setattr(agent, "MultilingualModel", detector)
    monkeypatch.setattr(agent.VectorSearch, "from_environment", vectors)
    monkeypatch.setattr(agent.silero.VAD, "load", Mock())
    proc = SimpleNamespace(userdata={})
    agent.prewarm(proc)
    # Constructing the detector requires a JobContext, absent during prewarm.
    detector.assert_not_called()
    assert "vad" in proc.userdata
    vectors.assert_called_once()
