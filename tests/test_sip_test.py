import asyncio
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import rtc
from test_dev_voice import harness  # noqa: F401
from test_structured_knowledge import content as knowledge_content  # noqa: F401

from clinic.resolver import ClinicUnavailable
from clinic.sip_test import CLINIC, NUMBER, RULE, TRUNK, SipTestConversation, ingress


def attributes():
    return {
        "sip.trunkPhoneNumber": NUMBER, "sip.trunkID": TRUNK,
        "sip.ruleID": RULE, "sip.callID": "SCL_test123",
        "sip.phoneNumber": "+12025550109",
    }


def test_exact_sip_ingress_uses_called_number_not_caller():
    route = ingress(rtc.ParticipantKind.PARTICIPANT_KIND_SIP, attributes())
    assert route.destination.called_number == NUMBER
    assert route.destination.trunk_id == TRUNK
    assert route.call_id == "SCL_test123"


@pytest.mark.parametrize("key,value", [
    ("sip.trunkPhoneNumber", ""), ("sip.trunkPhoneNumber", "+12025550109"),
    ("sip.trunkID", "wrong"), ("sip.ruleID", "wrong"),
    ("sip.callID", ""), ("sip.callID", "private\ntext"),
])
def test_reject_wrong_missing_or_spoofed_ingress(key, value):
    with pytest.raises(ClinicUnavailable):
        ingress(rtc.ParticipantKind.PARTICIPANT_KIND_SIP, attributes() | {key: value})


def test_non_sip_participant_and_wrong_attribute_spelling_rejected():
    with pytest.raises(ClinicUnavailable):
        ingress(rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, attributes())
    with pytest.raises(ClinicUnavailable):
        ingress(rtc.ParticipantKind.PARTICIPANT_KIND_SIP, {
            "sip.calledNumber": NUMBER, "sip.trunkId": TRUNK,
            "sip.phoneNumber": NUMBER, "sip.ruleID": RULE, "sip.callID": "abc",
        })


def test_sip_question_records_only_allowlisted_outcome(harness):  # noqa: F811
    async def exercise():
        h = harness()
        h.service.note = AsyncMock()
        conversation = SipTestConversation(h.call)
        assert "fictional" in conversation.greeting().text
        result = await conversation.turn(
            "I would like to know when would Dr Sharma be available today"
        )
        assert "Which doctor" in result.text
        h.service.note.assert_awaited_once_with(h.call.context, "availability", "ambiguous")
        h.service.note.reset_mock()
        result = await conversation.turn("Anaya Sharma")
        assert "published hours" in result.text
        h.service.note.assert_awaited_once_with(h.call.context, "availability", "success")
        assert not h.attempts

    asyncio.run(exercise())


def test_safety_summary_has_no_raw_question(harness):  # noqa: F811
    async def exercise():
        h = harness()
        h.service.note = AsyncMock()
        reply = await SipTestConversation(h.call).turn("What medicine should I take?")
        assert "cannot give medical advice" in reply.text
        h.service.note.assert_awaited_once_with(h.call.context, "clarify", "forbidden")

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", [
    "none", "start", "provider", "retryable", "summary", "wrong_trunk",
])
def test_worker_lifecycle_closes_database_session_and_media(monkeypatch, failure):
    path = Path(__file__).resolve().parents[1] / "scripts/clinic_sip.py"
    spec = importlib.util.spec_from_file_location("test_sip_worker", path)
    worker = importlib.util.module_from_spec(spec)
    disabled = logging.root.manager.disable
    try:
        spec.loader.exec_module(worker)
    finally:
        logging.disable(disabled)
    attrs = attributes()
    if failure == "wrong_trunk":
        attrs["sip.trunkID"] = "wrong"
    participant = SimpleNamespace(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP, attributes=attrs, identity="sip-test",
    )
    database = SimpleNamespace(open=AsyncMock(), close=AsyncMock())
    context = SimpleNamespace(session_id="safe-session-id")
    service = SimpleNamespace(start=AsyncMock(return_value=context), note=AsyncMock())
    if failure == "summary":
        service.note.side_effect = RuntimeError("private diagnostic")
    service.event = AsyncMock()
    call = SimpleNamespace(
        snapshot=SimpleNamespace(supported_languages=["en-IN", "hi-IN"]),
        start_watchdog=Mock(), close=AsyncMock(), _closed=False,
    )
    callbacks = {}
    session = SimpleNamespace(
        on=lambda event, callback: callbacks.update({event: callback}), aclose=AsyncMock(),
    )
    engine = SimpleNamespace(on=Mock(), aclose=AsyncMock())
    collector = SimpleNamespace(submit=Mock(), close=AsyncMock())
    agent = SimpleNamespace(play_fallback=AsyncMock(), provider_failed=Mock())
    room_callbacks = {}
    room = SimpleNamespace(
        name="call-controlled-test", remote_participants={participant.identity: participant},
        on=lambda event, callback: room_callbacks.update({event: callback}),
    )
    ctx = SimpleNamespace(
        room=room, proc=SimpleNamespace(userdata={"vad": object()}),
        api=SimpleNamespace(room=SimpleNamespace(delete_room=AsyncMock())),
        add_shutdown_callback=Mock(), shutdown=Mock(), connect=AsyncMock(),
        wait_for_participant=AsyncMock(return_value=participant),
    )
    scope = SimpleNamespace(clinic_id=CLINIC)
    for name, value in {
        "dotenv_values": lambda path: {}, "load_development": lambda *args: (CLINIC, object()),
        "database_settings": lambda: object(), "RuntimeDatabase": lambda _: database,
        "CallSessionService": lambda _: service, "PostgresResolutionRepository": lambda _: None,
        "ClinicResolver": lambda _: SimpleNamespace(resolve=AsyncMock(return_value=scope)),
        "CallOrchestrator": SimpleNamespace(load=AsyncMock(return_value=call)),
        "AgentSession": lambda **kwargs: session,
        "SipTestConversation": lambda _: object(),
        "DevelopmentVoiceAgent": lambda *args, **kwargs: agent,
        "UsageCollector": lambda _: collector, "load_audio": lambda _: (16000, b""),
    }.items():
        monkeypatch.setattr(worker, name, value)
    monkeypatch.setattr(worker.sarvam, "TTS", lambda **kwargs: engine)
    monkeypatch.setattr(worker.sarvam, "STT", lambda **kwargs: engine)
    monkeypatch.setattr(worker.noise_cancellation, "BVCTelephony", lambda: None)

    async def exercise():
        call.stop_event = asyncio.Event()
        agent.provider_failed.side_effect = call.stop_event.set

        async def start(**kwargs):
            assert kwargs["record"] is False
            assert kwargs["room_output_options"].transcription_enabled is False
            assert kwargs["room_input_options"].participant_identity == "sip-test"
            if failure == "start":
                raise RuntimeError("private startup error")
            if failure == "provider":
                callbacks["error"](None)
            else:
                if failure == "retryable":
                    callbacks["error"](SimpleNamespace(error=SimpleNamespace(
                        recoverable=True, error=RuntimeError("private provider payload"),
                    )))
                    assert not call.stop_event.is_set()
                    agent.provider_failed.assert_not_called()
                room_callbacks["participant_disconnected"](participant)

        session.start = AsyncMock(side_effect=start)
        await asyncio.wait_for(worker.entrypoint(ctx), 2)
        database.close.assert_awaited_once()
        if failure == "wrong_trunk":
            service.start.assert_not_awaited()
            ctx.api.room.delete_room.assert_not_awaited()
        else:
            assert service.start.await_args.kwargs["is_test"] is True
            ctx.api.room.delete_room.assert_awaited_once()
            if failure == "summary":
                service.event.assert_awaited_once_with(context, "ended")
            else:
                call.close.assert_awaited_once()
                session.aclose.assert_awaited_once()
                collector.close.assert_awaited_once()
        ctx.shutdown.assert_called_once()

    asyncio.run(exercise())