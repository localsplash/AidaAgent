import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aida_agent import worker
from aida_agent.config import DeploymentConfiguration
from conftest import CALL_ID


class EventSource:
    def __init__(self):
        self.handlers = {}

    def on(self, name):
        def register(callback):
            self.handlers[name] = callback
            return callback
        return register

    def off(self, name, callback):
        self.handlers.pop(name, None)


class FakeRoom(EventSource):
    def __init__(self):
        super().__init__()
        self.connected = False
        self.participant = Mock(publish_data=AsyncMock())

    @property
    def local_participant(self):
        if not self.connected:
            raise RuntimeError("cannot access participant before connect")
        return self.participant


def test_real_sdk_import_model_wiring_and_sip_options_without_network(monkeypatch, call):
    models = SimpleNamespace(STT=Mock(), LLM=Mock(), TTS=Mock())
    monkeypatch.setattr(worker, "inference", models)
    constructor = Mock()
    monkeypatch.setattr(worker, "AgentSession", constructor)
    config = DeploymentConfiguration("stt/model", "llm/model", "tts/model", "voice")
    worker.create_session(config, call, "fake-vad")
    models.STT.assert_called_once_with(model="stt/model", language="en")
    models.LLM.assert_called_once_with(model="llm/model")
    models.TTS.assert_called_once_with(model="tts/model", voice="voice", language="en")
    assert constructor.call_args.kwargs["vad"] == "fake-vad"
    assert constructor.call_args.kwargs["turn_handling"]["interruption"]["enabled"] is True


@pytest.fixture
def runtime(monkeypatch, metadata):
    session = EventSource()
    session.start, session.aclose, session.say = AsyncMock(), AsyncMock(), Mock()
    session.generate_reply = Mock()
    session.input, session.output = Mock(), Mock()
    session.interrupt, session.shutdown = Mock(), Mock()
    session.current_agent = Mock()
    room = FakeRoom()
    room.name = f"aida-{CALL_ID}"
    room.disconnect = AsyncMock()
    ctx = SimpleNamespace(
        job=SimpleNamespace(metadata=json.dumps(metadata)), room=room,
        proc=SimpleNamespace(userdata={"vad": "fake-vad"}),
        shutdown=Mock(), connect=AsyncMock(), add_shutdown_callback=Mock(),
    )
    async def connect():
        room.connected = True
    ctx.connect.side_effect = connect
    monkeypatch.setattr(worker, "create_session", Mock(return_value=session))
    for key in ("AIDA_STT_MODEL", "AIDA_LLM_MODEL", "AIDA_TTS_MODEL", "AIDA_TTS_VOICE"):
        monkeypatch.setenv(key, "fake/model")
    return ctx, session


async def test_worker_sdk_events_publish_handset_envelopes_and_cleanup(runtime):
    ctx, session = runtime
    await worker.entrypoint(ctx)
    options = session.start.call_args.kwargs["room_options"]
    assert options.text_input is False
    assert options.delete_room_on_close is False
    assert options.participant_kinds == [worker.rtc.ParticipantKind.PARTICIPANT_KIND_SIP]
    assert session.start.call_args.kwargs["record"] is False
    session.say.assert_called_once_with("Thank you for calling.", allow_interruptions=True)
    session.handlers["user_input_transcribed"](
        SimpleNamespace(transcript="Caller words", is_final=True))
    session.handlers["conversation_item_added"](SimpleNamespace(
        item=SimpleNamespace(role="user", text_content="Caller words", id="user-1")))
    session.handlers["conversation_item_added"](SimpleNamespace(
        item=SimpleNamespace(role="assistant", text_content="Agent words", id="agent-1")))
    import asyncio
    for _ in range(20):
        if ctx.room.local_participant.publish_data.await_count == 2:
            break
        await asyncio.sleep(0)
    events = [json.loads(args.args[0]) for args in
              ctx.room.local_participant.publish_data.await_args_list]
    assert [event["text"] for event in events] == ["Caller words", "Agent words"]
    assert [event["sequence"] for event in events] == [1, 2]
    await ctx.add_shutdown_callback.call_args.args[0]()
    session.aclose.assert_awaited_once()
    assert "data_received" not in ctx.room.handlers


async def test_invalid_job_is_rejected_before_room_or_provider_access(runtime, caplog):
    ctx, session = runtime
    ctx.job.metadata = '{"apiKey":"secret-credential"}'
    await worker.entrypoint(ctx)
    ctx.shutdown.assert_called_once_with(reason="invalid configuration")
    ctx.connect.assert_not_awaited()
    worker.create_session.assert_not_called()
    assert "secret-credential" not in caplog.text


async def test_takeover_during_connect_prevents_session_and_greeting(runtime):
    from test_control import packet
    ctx, session = runtime
    async def connect():
        ctx.room.handlers["data_received"](packet())
    ctx.connect.side_effect = connect
    await worker.entrypoint(ctx)
    session.start.assert_not_awaited()
    session.say.assert_not_called()
    session.generate_reply.assert_not_called()
    await ctx.add_shutdown_callback.call_args.args[0]()
    ctx.room.disconnect.assert_awaited_once()


async def test_missing_opening_still_greets_the_caller(runtime):
    ctx, session = runtime
    metadata = json.loads(ctx.job.metadata)
    del metadata["openingStatement"]
    ctx.job.metadata = json.dumps(metadata)
    await worker.entrypoint(ctx)
    session.generate_reply.assert_called_once()
    session.say.assert_not_called()
    await ctx.add_shutdown_callback.call_args.args[0]()


def test_silero_model_loads_from_installed_package_offline():
    proc = SimpleNamespace(userdata={})
    worker.prewarm(proc)
    assert proc.userdata["vad"] is not None


def test_real_session_options_without_provider_requests():
    # Concrete AgentSession construction validates the pinned API option shape.
    # No start(), HTTP request, LiveKit connection, or inference is performed.
    session = worker.AgentSession(
        turn_handling={"turn_detection": "vad", "interruption": {"enabled": True},
                       "preemptive_generation": {"enabled": False}},
    )
    assert session is not None


async def test_real_inference_constructors_without_network(monkeypatch, call):
    monkeypatch.setenv("LIVEKIT_API_KEY", "offline-test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "offline-test-secret-with-at-least-32-bytes")
    config = DeploymentConfiguration(
        "deepgram/nova-3-general", "openai/gpt-4.1-mini", "cartesia/sonic-3",
        "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
    )
    session = worker.create_session(config, call, None)
    await session.stt.aclose()
    await session.llm.aclose()
    await session.tts.aclose()
