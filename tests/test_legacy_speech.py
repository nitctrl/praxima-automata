"""Protect the restored voice pipeline while adding only clinic knowledge."""

import ast
from pathlib import Path
from unittest.mock import Mock

from livekit.agents.voice import Agent


def test_original_voice_hooks_are_not_overridden():
    from praxima.entrypoints.voice_worker import VoiceAgent

    assert VoiceAgent.tts_node is Agent.tts_node
    assert VoiceAgent.llm_node is Agent.llm_node
    # The one deliberate override: deterministic safety routing before the model.
    assert VoiceAgent.on_user_turn_completed is not Agent.on_user_turn_completed


def test_agent_starts_without_tools_while_knowledge_loads(monkeypatch):
    from praxima.entrypoints import voice_worker as agent

    captured = {}
    monkeypatch.setattr(agent.Agent, "__init__", lambda self, **kw: captured.update(kw))
    for provider, name in [(agent.sarvam, "STT"), (agent.sarvam, "TTS"), (agent.google, "LLM")]:
        monkeypatch.setattr(provider, name, Mock())
    monkeypatch.delenv("SARVAM_STT_LANGUAGE", raising=False)
    agent.VoiceAgent(telephony=True)
    # Knowledge loads in parallel; its tools and prompt are attached once it arrives.
    assert captured["tools"] == []
    assert not hasattr(agent, "lookup_info")
    assert captured["instructions"] == agent.flow.LOADING_INSTRUCTIONS
    assert agent.sarvam.STT.call_args.kwargs["language"] == "unknown"  # per-utterance detection
    assert captured["min_endpointing_delay"] == 0.45
    assert captured["max_endpointing_delay"] == 1.2
    assert agent.google.LLM.call_args.kwargs["temperature"] == 0


def test_no_clinic_orchestration_or_custom_speech_imported_by_agent():
    worker = Path(__file__).resolve().parents[1] / "src/praxima/entrypoints/voice_worker.py"
    tree = ast.parse(worker.read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not modules.intersection(
        {
            "praxima.dev.dev_voice",
            "praxima.modules.engagement.application.sessions",
            "praxima.runtime.usage",
        }
    )
