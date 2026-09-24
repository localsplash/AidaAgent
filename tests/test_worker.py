import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aida_agent import worker
from aida_agent.bootstrap import authorized_profile
from aida_agent.config import DeploymentConfiguration
from conftest import CALL_ID, CONTEXT, PBX_INSTANCE_ID


class EventSource:
    def __init__(self):
        self.handlers = {}

    def on(self, name, callback=None):
        def register(callback):
            self.handlers[name] = callback
            return callback
        return register(callback) if callback else register

    def off(self, name, callback):
        self.handlers.pop(name, None)


class FakeRoom(EventSource):
    def __init__(self):
        super().__init__()
        self.connected = False
        self.participant = Mock(publish_data=AsyncMock(), identity="agent-1", sid="PA_agent")
        self.sip = SimpleNamespace(
            identity="sip-caller", sid="PA_sip",
            kind=worker.rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
            attributes={"sip.aidaRouteToken": "r" * 43},
            track_publications={},
        )
        self.audio = SimpleNamespace(
            sid="TR_sip_audio", kind=worker.rtc.TrackKind.KIND_AUDIO,
            source=worker.rtc.TrackSource.SOURCE_MICROPHONE, track=None,
        )
        def subscribed(enabled):
            self.audio.track = object() if enabled else None
            if enabled:
                self.handlers["track_subscribed"](self.audio.track, self.audio, self.sip)
        self.audio.set_subscribed = Mock(side_effect=subscribed)
        self.sip.track_publications[self.audio.sid] = self.audio
        self.remote_participants = {self.sip.identity: self.sip}

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
def runtime(monkeypatch, dispatch, call):
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
        job=SimpleNamespace(metadata=json.dumps(dispatch)), room=room,
        proc=SimpleNamespace(userdata={"vad": "fake-vad"}),
        shutdown=Mock(), connect=AsyncMock(), add_shutdown_callback=Mock(),
    )
    async def connect(**kwargs):
        room.connected = True
    ctx.connect.side_effect = connect
    monkeypatch.setattr(worker, "create_session", Mock(return_value=session))
    monkeypatch.setattr(worker, "BootstrapClient", Mock(return_value=SimpleNamespace(
        authorize=AsyncMock(return_value=call))))
    monkeypatch.setenv("OFFICEPULSE_API_BASE_URL", "https://officepulse.test")
    monkeypatch.setenv("AIDA_ROUTE_TOKEN_ATTRIBUTE", "sip.aidaRouteToken")
    for key in ("AIDA_STT_MODEL", "AIDA_LLM_MODEL", "AIDA_TTS_MODEL", "AIDA_TTS_VOICE"):
        monkeypatch.setenv(key, "fake/model")
    return ctx, session


async def test_worker_sdk_events_publish_handset_envelopes_and_cleanup(runtime, contract):
    ctx, session = runtime
    await worker.entrypoint(ctx)
    options = session.start.call_args.kwargs["room_options"]
    assert options.participant_identity == "sip-caller"
    assert session.start.call_args.kwargs["session_host"] is False
    assert ctx.connect.call_args.kwargs["auto_subscribe"] == worker.AutoSubscribe.SUBSCRIBE_NONE
    ready = ctx.room.local_participant.publish_data.await_args_list[0]
    ctx.room.audio.set_subscribed.assert_called_once_with(True)
    assert ready.kwargs == {"topic": "aida.event.agent_ready", "reliable": True}
    assert json.loads(ready.args[0]) == contract["ready"]
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
        if ctx.room.local_participant.publish_data.await_count == 3:
            break
        await asyncio.sleep(0)
    events = [json.loads(args.args[0]) for args in
              ctx.room.local_participant.publish_data.await_args_list[1:]]
    assert [event["text"] for event in events] == ["Caller words", "Agent words"]
    assert [event["sequence"] for event in events] == [1, 2]
    await ctx.add_shutdown_callback.call_args.args[0]()
    session.aclose.assert_awaited_once()
    assert "data_received" not in ctx.room.handlers


@pytest.mark.parametrize("job_metadata", [
    '{"apiKey":"secret-credential"}',
    json.dumps({"callSessionId": CALL_ID, "bootstrapToken": "b" * 43}),
    json.dumps({"callSessionId": CALL_ID, "bootstrapToken": "b" * 43,
                "pbxInstanceId": PBX_INSTANCE_ID, "context": "from carrier"}),
], ids=["unknown-field", "v1-dispatch-without-scope", "bad-context-grammar"])
async def test_invalid_job_is_rejected_before_room_or_provider_access(runtime, caplog, job_metadata):
    ctx, session = runtime
    ctx.job.metadata = job_metadata
    await worker.entrypoint(ctx)
    ctx.shutdown.assert_called_once_with(reason="invalid configuration")
    ctx.connect.assert_not_awaited()
    worker.create_session.assert_not_called()
    assert "secret-credential" not in caplog.text


@pytest.fixture
def fixture_authority(runtime, contract):
    # Real authorized_profile over the shared v2 fixture, bound to whatever dispatch/leg the
    # worker actually passes: exercises dispatch → bootstrap → scope check end to end.
    ctx, _session = runtime
    ctx.job.metadata = json.dumps(contract["dispatch"])

    async def authorize(dispatch, room_name, leg):
        return authorized_profile(json.dumps(contract["response"]).encode(), dispatch, room_name, leg)

    worker.BootstrapClient.return_value.authorize.side_effect = authorize
    return contract


async def test_context_scoped_dispatch_completes_conversation_without_tenant(
        runtime, fixture_authority, caplog):
    import logging
    ctx, session = runtime
    caplog.set_level(logging.INFO, logger="aida_agent")
    del fixture_authority["response"]["profileSnapshot"]["tenantId"]
    await worker.entrypoint(ctx)
    call = worker.create_session.call_args.args[1]
    assert call.tenant_id == ""
    assert (call.pbx_instance_id, call.context) == ("officepulse-dev", "example-office")
    ctx.room.participant.publish_data.assert_awaited_once()
    assert json.loads(ctx.room.participant.publish_data.await_args.args[0]) == fixture_authority["ready"]
    assert session.input.set_audio_enabled.call_args.args == (True,)
    assert session.output.set_audio_enabled.call_args.args == (True,)
    session.say.assert_called_once_with("Thank you for calling.", allow_interruptions=True)
    ctx.shutdown.assert_not_called()
    enabled = next(r for r in caplog.records if getattr(r, "event", None) == "conversation-enabled")
    assert (enabled.pbxInstanceId, enabled.context) == ("officepulse-dev", "example-office")
    await ctx.add_shutdown_callback.call_args.args[0]()


async def test_context_scoped_dispatch_with_tenant_completes_conversation(runtime, fixture_authority):
    ctx, session = runtime
    await worker.entrypoint(ctx)
    assert worker.create_session.call_args.args[1].tenant_id == "42"
    session.say.assert_called_once_with("Thank you for calling.", allow_interruptions=True)
    await ctx.add_shutdown_callback.call_args.args[0]()


@pytest.mark.parametrize("key,value", [
    ("pbxInstanceId", "officepulse-prod"), ("context", "other-office"),
], ids=["same-context-other-instance", "other-context"])
async def test_scope_mismatch_between_dispatch_and_profile_fails_closed(
        runtime, fixture_authority, caplog, key, value):
    import logging
    ctx, session = runtime
    caplog.set_level(logging.INFO, logger="aida_agent")
    dispatch = {**fixture_authority["dispatch"], key: value}
    ctx.job.metadata = json.dumps(dispatch)
    await worker.entrypoint(ctx)
    worker.BootstrapClient.return_value.authorize.assert_awaited_once()
    worker.create_session.assert_not_called()
    ctx.room.participant.publish_data.assert_not_awaited()
    session.say.assert_not_called()
    ctx.shutdown.assert_called()
    failed = next(r for r in caplog.records if getattr(r, "event", None) == "startup-failed")
    assert (failed.stage, failed.errorType) == ("bootstrap", "InvalidConfiguration")
    assert (failed.pbxInstanceId, failed.context) == (dispatch["pbxInstanceId"], dispatch["context"])


async def test_scope_comes_from_dispatch_not_participant_attributes(runtime, fixture_authority,
                                                                    caplog):
    import logging
    ctx, session = runtime
    caplog.set_level(logging.INFO, logger="aida_agent")
    ctx.room.sip.attributes.update({
        "context": "impostor-context", "pbxInstanceId": "impostor-pbx",
        "sip.phoneNumber": "+15550000000", "sip.trunkPhoneNumber": "+15551234567",
    })
    await worker.entrypoint(ctx)
    session.say.assert_called_once()
    records = [r for r in caplog.records if getattr(r, "event", None)]
    assert records and all((r.pbxInstanceId, r.context) == (PBX_INSTANCE_ID, CONTEXT)
                           for r in records)
    text = str([r.__dict__ for r in caplog.records])
    assert "impostor" not in text
    assert "+1555" not in text
    await ctx.add_shutdown_callback.call_args.args[0]()


async def test_takeover_during_connect_prevents_session_and_greeting(runtime):
    from test_control import packet
    ctx, session = runtime
    async def connect(**kwargs):
        ctx.room.handlers["data_received"](packet())
    ctx.connect.side_effect = connect
    await worker.entrypoint(ctx)
    session.start.assert_not_awaited()
    session.say.assert_not_called()
    session.generate_reply.assert_not_called()
    await ctx.add_shutdown_callback.call_args.args[0]()
    ctx.room.disconnect.assert_awaited()


async def test_missing_opening_still_greets_the_caller(runtime):
    from dataclasses import replace
    ctx, session = runtime
    authorize = worker.BootstrapClient.return_value.authorize
    authorize.return_value = replace(authorize.return_value, opening_statement="")
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
        "deepgram/nova-3-general", "google/gemma-4-31b-it", "deepgram/aura-2",
        "asteria",
    )
    session = worker.create_session(config, call, None)
    await session.stt.aclose()
    await session.llm.aclose()
    await session.tts.aclose()


async def test_audio_and_greeting_wait_for_ready_delivery(runtime):
    ctx, session = runtime

    async def publish(*args, **kwargs):
        assert kwargs["topic"] == "aida.event.agent_ready"
        session.start.assert_awaited_once()
        assert ctx.room.audio.track is not None
        session.input.set_audio_enabled.assert_called_once_with(False)
        session.output.set_audio_enabled.assert_called_once_with(False)
        session.say.assert_not_called()
        session.generate_reply.assert_not_called()

    ctx.room.participant.publish_data.side_effect = publish
    await worker.entrypoint(ctx)
    assert session.input.set_audio_enabled.call_args.args == (True,)
    session.say.assert_called_once()
    ctx.room.participant.publish_data.assert_awaited_once()
    await ctx.add_shutdown_callback.call_args.args[0]()


@pytest.mark.parametrize("phase", ["authorize", "start", "ready"])
async def test_startup_failure_never_enables_audio(runtime, phase, caplog):
    ctx, session = runtime
    targets = {"authorize": worker.BootstrapClient.return_value.authorize,
               "start": session.start, "ready": ctx.room.participant.publish_data}
    targets[phase].side_effect = RuntimeError("secret-bootstrap-and-prompt")
    await worker.entrypoint(ctx)
    session.say.assert_not_called()
    session.generate_reply.assert_not_called()
    assert all(call.args == (False,) for call in session.input.set_audio_enabled.call_args_list)
    ctx.shutdown.assert_called()
    ctx.room.disconnect.assert_awaited()
    assert "secret-bootstrap-and-prompt" not in caplog.text
    assert not ctx.room.handlers
    if phase == "authorize":
        worker.create_session.assert_not_called()


async def test_delayed_participant_and_attribute(runtime):
    import asyncio
    ctx, session = runtime
    ctx.room.remote_participants.clear()
    task = asyncio.create_task(worker.entrypoint(ctx))
    try:
        while not ctx.room.connected:
            await asyncio.sleep(0)
        ctx.room.sip.attributes.clear()
        ctx.room.remote_participants[ctx.room.sip.identity] = ctx.room.sip
        ctx.room.handlers["participant_connected"](ctx.room.sip)
        await asyncio.sleep(0)
        worker.BootstrapClient.return_value.authorize.assert_not_awaited()
        ctx.room.sip.attributes["sip.aidaRouteToken"] = "r" * 43
        ctx.room.handlers["participant_attributes_changed"]({}, ctx.room.sip)
        await asyncio.wait_for(task, timeout=1)
        session.say.assert_called_once()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await ctx.add_shutdown_callback.call_args.args[0]()


@pytest.mark.parametrize("mode", ["no-sip", "no-token", "invalid-token", "multiple-sip"])
async def test_missing_or_ambiguous_sip_fails_before_providers(runtime, monkeypatch, mode):
    ctx, session = runtime
    monkeypatch.setenv("AIDA_BOOTSTRAP_TIMEOUT_SECONDS", "1")
    if mode == "no-sip":
        ctx.room.remote_participants.clear()
    elif mode == "no-token":
        ctx.room.sip.attributes.clear()
    elif mode == "invalid-token":
        ctx.room.sip.attributes["sip.aidaRouteToken"] = "bad"
    else:
        ctx.room.remote_participants["other"] = SimpleNamespace(
            kind=worker.rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
    await worker.entrypoint(ctx)
    worker.create_session.assert_not_called()
    ctx.room.participant.publish_data.assert_not_awaited()
    ctx.shutdown.assert_called()


@pytest.mark.parametrize("change", ["sid", "identity", "token", "disconnect", "extra-sip"])
async def test_leg_change_during_authorization_fails_closed(runtime, change):
    ctx, session = runtime
    authorize = worker.BootstrapClient.return_value.authorize
    profile = authorize.return_value

    async def replace_leg(*args):
        if change == "sid":
            ctx.room.sip.sid = "PA_replacement"
        elif change == "identity":
            ctx.room.sip.identity = "impostor"
        elif change == "token":
            ctx.room.sip.attributes["sip.aidaRouteToken"] = "x" * 43
        elif change == "disconnect":
            ctx.room.handlers["participant_disconnected"](ctx.room.sip)
        else:
            ctx.room.remote_participants["other"] = SimpleNamespace(
                kind=worker.rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
        return profile

    authorize.side_effect = replace_leg
    await worker.entrypoint(ctx)
    worker.create_session.assert_not_called()
    ctx.room.participant.publish_data.assert_not_awaited()
    ctx.shutdown.assert_called()


@pytest.mark.parametrize("phase", ["authorize", "start", "ready"])
async def test_human_takeover_during_startup_cancels_pending_work(runtime, phase):
    import asyncio
    from test_control import packet
    ctx, session = runtime

    async def takeover(*args, **kwargs):
        ctx.room.handlers["data_received"](packet())
        await asyncio.Event().wait()

    targets = {"authorize": worker.BootstrapClient.return_value.authorize,
               "start": session.start, "ready": ctx.room.participant.publish_data}
    targets[phase].side_effect = takeover
    await asyncio.wait_for(worker.entrypoint(ctx), timeout=1)
    session.say.assert_not_called()
    session.generate_reply.assert_not_called()
    assert all(call.args == (False,) for call in session.input.set_audio_enabled.call_args_list)
    ctx.shutdown.assert_called()


async def test_transfer_failure_during_startup_cannot_enable_audio(runtime):
    from test_control import packet
    ctx, session = runtime

    async def start(**kwargs):
        command = packet("transfer_failed", deadlineMs=9_000_000_000_000)
        ctx.room.handlers["data_received"](command)
        session.input.set_audio_enabled.assert_called_once_with(False)
        session.say.assert_not_called()

    session.start.side_effect = start
    await worker.entrypoint(ctx)
    session.say.assert_called_once_with("Thank you for calling.", allow_interruptions=True)
    await ctx.add_shutdown_callback.call_args.args[0]()


async def test_transfer_requested_during_startup_plays_profile_outro_instead_of_greeting(runtime):
    import asyncio
    from dataclasses import replace
    from test_control import packet
    ctx, session = runtime
    call = replace(worker.BootstrapClient.return_value.authorize.return_value,
                   transfer_statement="I will connect you now.")
    worker.BootstrapClient.return_value.authorize.return_value = call
    session.interrupt.return_value = asyncio.get_running_loop().create_future()
    session.interrupt.return_value.set_result(None)
    session.say.return_value = Mock(wait_for_playout=AsyncMock())

    async def start(**kwargs):
        ctx.room.handlers["data_received"](
            packet("transfer_requested", deadlineMs=9_000_000_000_000))
        session.say.assert_not_called()
        assert all(call.args == (False,) for call in session.input.set_audio_enabled.call_args_list)

    session.start.side_effect = start
    await worker.entrypoint(ctx)
    for _ in range(10):
        await asyncio.sleep(0)
    session.say.assert_called_once_with(call.transfer_statement, allow_interruptions=False)
    session.generate_reply.assert_not_called()
    assert all(call.args == (False,) for call in session.input.set_audio_enabled.call_args_list)
    await ctx.add_shutdown_callback.call_args.args[0]()


async def test_ready_worker_keeps_outro_audible_after_handset_answers(runtime):
    import asyncio
    from dataclasses import replace
    from test_control import packet
    ctx, session = runtime
    worker.BootstrapClient.return_value.authorize.return_value = replace(
        worker.BootstrapClient.return_value.authorize.return_value,
        transfer_statement="Connecting you now.")
    await worker.entrypoint(ctx)
    interrupted = asyncio.get_running_loop().create_future()
    interrupted.set_result(None)
    session.interrupt.return_value = interrupted
    started, finished = asyncio.Event(), asyncio.Event()

    async def playout():
        started.set()
        await finished.wait()

    session.say.return_value = Mock(wait_for_playout=AsyncMock(side_effect=playout))
    ctx.room.handlers['data_received'](
        packet('transfer_requested', deadlineMs=9_000_000_000_000))
    await started.wait()
    ctx.room.handlers['data_received'](
        packet('human_answered', commandId='answer:2', deadlineMs=9_000_000_000_000))
    await asyncio.sleep(0)
    assert session.output.set_audio_enabled.call_args.args == (True,)
    ctx.shutdown.assert_not_called()
    ctx.room.disconnect.assert_not_awaited()
    finished.set()
    for _ in range(30):
        if ctx.shutdown.called:
            break
        await asyncio.sleep(0)
    ctx.room.disconnect.assert_awaited_once()
    ctx.shutdown.assert_called()
    await ctx.add_shutdown_callback.call_args.args[0]()


async def test_sip_replacement_after_ready_silences_session(runtime):
    ctx, session = runtime
    await worker.entrypoint(ctx)
    ctx.room.sip.sid = "PA_replacement"
    ctx.room.handlers["participant_connected"](ctx.room.sip)
    assert session.input.set_audio_enabled.call_args.args == (False,)
    assert session.output.set_audio_enabled.call_args.args == (False,)
    ctx.shutdown.assert_called()
    await ctx.add_shutdown_callback.call_args.args[0]()


async def test_external_shutdown_cancels_admission(runtime):
    import asyncio
    ctx, session = runtime
    ctx.room.remote_participants.clear()
    task = asyncio.create_task(worker.entrypoint(ctx))
    while not ctx.room.connected:
        await asyncio.sleep(0)
    await ctx.add_shutdown_callback.call_args.args[0]()
    await asyncio.wait_for(task, timeout=1)
    worker.create_session.assert_not_called()
    assert not ctx.room.handlers


async def test_sdk_audio_gate_survives_attaching_room_streams():
    # Exercise the installed SDK's public IO setters, not mocks of those setters.
    from livekit.agents.voice import io
    input_stream = Mock(spec=io.AudioInput)
    output_stream = Mock(spec=io.AudioOutput)
    session = worker.AgentSession()
    session.input.set_audio_enabled(False)
    session.output.set_audio_enabled(False)
    session.input.audio = input_stream
    session.output.audio = output_stream
    assert session.input.audio_enabled is False
    assert session.output.audio_enabled is False
    input_stream.on_attached.assert_not_called()
    input_stream.on_detached.assert_called_once()


@pytest.mark.parametrize("phase", ["connect", "authorize", "start", "ready"])
async def test_one_deadline_covers_each_startup_phase(runtime, monkeypatch, phase):
    import asyncio
    ctx, session = runtime
    monkeypatch.setenv("AIDA_BOOTSTRAP_TIMEOUT_SECONDS", "1")

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    targets = {"connect": ctx.connect, "authorize": worker.BootstrapClient.return_value.authorize,
               "start": session.start, "ready": ctx.room.participant.publish_data}
    targets[phase].side_effect = hang
    await asyncio.wait_for(worker.entrypoint(ctx), timeout=2)
    session.say.assert_not_called()
    assert all(call.args == (False,) for call in session.input.set_audio_enabled.call_args_list)
    ctx.shutdown.assert_called()


async def test_subscription_is_scoped_to_bootstrapped_sip_microphone(runtime):
    ctx, session = runtime
    room = ctx.room
    video = SimpleNamespace(sid="TR_video", kind=worker.rtc.TrackKind.KIND_VIDEO,
                            source=worker.rtc.TrackSource.SOURCE_CAMERA, track=None,
                            set_subscribed=Mock())
    screen_audio = SimpleNamespace(sid="TR_screen", kind=worker.rtc.TrackKind.KIND_AUDIO,
                                  source=worker.rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO,
                                  track=None, set_subscribed=Mock())
    room.sip.track_publications.update({p.sid: p for p in (video, screen_audio)})
    other_audio = SimpleNamespace(sid="TR_other", kind=worker.rtc.TrackKind.KIND_AUDIO,
                                 source=worker.rtc.TrackSource.SOURCE_MICROPHONE,
                                 track=None, set_subscribed=Mock())
    other = SimpleNamespace(identity="handset", sid="PA_handset",
                            kind=worker.rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
                            track_publications={other_audio.sid: other_audio})
    room.remote_participants[other.identity] = other
    authorize = worker.BootstrapClient.return_value.authorize
    profile = authorize.return_value

    async def authorize_before_audio(*args):
        room.handlers["track_published"](room.audio, room.sip)
        room.audio.set_subscribed.assert_not_called()
        return profile

    authorize.side_effect = authorize_before_audio
    await worker.entrypoint(ctx)
    room.handlers["track_published"](other_audio, other)
    room.audio.set_subscribed.assert_called_once_with(True)
    for publication in (video, screen_audio, other_audio):
        publication.set_subscribed.assert_not_called()
    await ctx.add_shutdown_callback.call_args.args[0]()
    assert room.audio.set_subscribed.call_args.args == (False,)


async def test_ready_waits_for_late_audio_publication_and_subscription_ack(runtime):
    import asyncio
    ctx, session = runtime
    room = ctx.room
    room.sip.track_publications.clear()
    room.audio.set_subscribed.side_effect = None
    task = asyncio.create_task(worker.entrypoint(ctx))
    try:
        while not session.start.await_count:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        room.participant.publish_data.assert_not_awaited()
        session.say.assert_not_called()
        room.sip.track_publications[room.audio.sid] = room.audio
        room.handlers["track_published"](room.audio, room.sip)
        await asyncio.sleep(0)
        room.audio.set_subscribed.assert_called_once_with(True)
        room.participant.publish_data.assert_not_awaited()
        room.audio.track = object()
        room.handlers["track_subscribed"](room.audio.track, room.audio, room.sip)
        await asyncio.wait_for(task, 1)
        room.participant.publish_data.assert_awaited_once()
        session.say.assert_called_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctx.add_shutdown_callback.call_args.args[0]()


@pytest.mark.parametrize("failure", ["timeout", "subscription-failed", "takeover"])
async def test_unavailable_audio_never_signals_readiness(runtime, monkeypatch, failure, caplog):
    import asyncio
    from test_control import packet
    ctx, session = runtime
    monkeypatch.setenv("AIDA_BOOTSTRAP_TIMEOUT_SECONDS", "1")
    room = ctx.room
    room.audio.set_subscribed.side_effect = None
    task = asyncio.create_task(worker.entrypoint(ctx))
    try:
        while not room.audio.set_subscribed.call_count:
            await asyncio.sleep(0)
        if failure == "subscription-failed":
            room.handlers["track_subscription_failed"](room.sip, room.audio.sid,
                                                        "private-provider-details")
        elif failure == "takeover":
            room.handlers["data_received"](packet())
        await asyncio.wait_for(task, 2)
        room.participant.publish_data.assert_not_awaited()
        session.say.assert_not_called()
        ctx.shutdown.assert_called()
        assert "private-provider-details" not in caplog.text
        assert all(c.args == (False,) for c in session.input.set_audio_enabled.call_args_list)
        assert not room.handlers
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_lost_authorized_audio_ends_ready_session(runtime):
    ctx, session = runtime
    await worker.entrypoint(ctx)
    ctx.room.handlers["track_unsubscribed"](ctx.room.audio.track, ctx.room.audio, ctx.room.sip)
    ctx.shutdown.assert_called()
    assert session.input.set_audio_enabled.call_args.args == (False,)
    assert session.output.set_audio_enabled.call_args.args == (False,)
    await ctx.add_shutdown_callback.call_args.args[0]()


async def test_lifecycle_diagnostics_count_stt_without_call_content(runtime, caplog):
    import logging
    ctx, session = runtime
    caplog.set_level(logging.INFO, logger="aida_agent")
    await worker.entrypoint(ctx)
    session.handlers["user_input_transcribed"](
        SimpleNamespace(transcript="private-caller-words", is_final=True))
    session.handlers["conversation_item_added"](SimpleNamespace(
        item=SimpleNamespace(role="assistant", text_content="private-agent-words", id="item")))
    session.handlers["error"](SimpleNamespace(error=SimpleNamespace(
        recoverable=True, message="private-provider-details")))
    await ctx.add_shutdown_callback.call_args.args[0]()
    closing = next(r for r in caplog.records if getattr(r, "event", None) == "closing")
    assert closing.sttFinalEvents == closing.assistantItems == 1
    assert closing.callSessionId == CALL_ID
    assert (closing.pbxInstanceId, closing.context) == (PBX_INSTANCE_ID, CONTEXT)
    records = str([r.__dict__ for r in caplog.records])
    assert "42" not in json.dumps([getattr(r, "tenantId", None) for r in caplog.records])
    assert "Ask how we can help." not in records
    assert "private-caller-words" not in records
    assert "private-agent-words" not in records
    assert "private-provider-details" not in records
