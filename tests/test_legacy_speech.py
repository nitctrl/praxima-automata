"""Protect the restored voice pipeline while adding only clinic knowledge."""

import ast
from pathlib import Path
from unittest.mock import Mock

from livekit.agents.voice import Agent


def test_original_voice_hooks_are_not_overridden():
    from agent import VoiceAgent

    assert VoiceAgent.tts_node is Agent.tts_node
    assert VoiceAgent.on_user_turn_completed is Agent.on_user_turn_completed
    assert VoiceAgent.llm_node is Agent.llm_node


def test_only_read_only_clinic_tools_are_attached(monkeypatch):
    import agent

    captured = {}
    monkeypatch.setattr(agent.Agent, "__init__", lambda self, **kw: captured.update(kw))
    for provider, name in [(agent.sarvam, "STT"), (agent.sarvam, "TTS"), (agent.anthropic, "LLM")]:
        monkeypatch.setattr(provider, name, Mock())
    agent.VoiceAgent(telephony=True)
    assert len(captured["tools"]) == 9
    assert agent.lookup_info not in captured["tools"]
    assert "information-only" in captured["instructions"]
    assert captured["min_endpointing_delay"] == 0.45
    assert captured["max_endpointing_delay"] == 1.2
    assert agent.anthropic.LLM.call_args.kwargs["temperature"] == 0.7


def test_no_clinic_orchestration_or_custom_speech_imported_by_agent():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "src/agent.py").read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not modules.intersection({"clinic.dev_voice", "clinic.sessions", "clinic.usage"})