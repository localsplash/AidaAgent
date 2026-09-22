import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aida_agent.control import ControlHandler
from conftest import CALL_ID


def packet(action="human_answered", **values):
    return SimpleNamespace(topic="aida.control", participant=None, data=json.dumps({
        "type": "control", "callId": CALL_ID, "commandId": "bridge:1",
        "action": action, "deadlineMs": 11000, **values,
    }).encode())


@pytest.fixture
def control():
    session = Mock()
    return ControlHandler(CALL_ID, session, AsyncMock(), Mock(), now_ms=lambda: 1000,
                          failed_statement="May I take a message?")


async def test_takeover_silences_before_disconnect_and_is_idempotent(control):
    assert control.receive(packet())
    assert control.stopping
    control.session.input.set_audio_enabled.assert_called_once_with(False)
    control.session.output.set_audio_enabled.assert_called_once_with(False)
    control.session.interrupt.assert_called_once_with(force=True)
    control.session.shutdown.assert_called_once_with(drain=False)
    assert not control.receive(packet())
    assert not control.receive(packet("transfer_failed", commandId="failure:2"))
    control.session.say.assert_not_called()
    await control.close()
    control.disconnect.assert_awaited_once_with()
    control.shutdown.assert_called_once()


async def test_participant_cannot_spoof_server_and_does_not_consume_command_id(control):
    untrusted = packet()
    untrusted.participant = SimpleNamespace(identity="officepulse-integration")
    assert not control.receive(untrusted)
    assert not control.stopping
    assert control.receive(packet())
    await control.close()


@pytest.mark.parametrize("values", [
    {"callId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}, {"type": "transcript"},
    {"deadlineMs": True}, {"deadlineMs": 0}, {"deadlineMs": 1.5},
    {"deadlineMs": "11000"}, {"commandId": ""}, {"commandId": "x" * 129},
    {"action": "delete_room"}, {"token": "private"},
])
def test_bad_controls_are_ignored(control, values):
    assert not control.receive(packet(**values))
    control.session.interrupt.assert_not_called()
    control.shutdown.assert_not_called()


def test_other_topic_and_malformed_json_ignored(control):
    other = packet()
    other.topic = "lk.chat"
    assert not control.receive(other)
    other.topic = "aida.control"
    other.data = b"{" + b"x" * 3000
    assert not control.receive(other)


async def test_expired_takeover_stops_immediately_even_if_disconnect_hangs(control):
    async def hang():
        await asyncio.Event().wait()
    control.disconnect = AsyncMock(side_effect=hang)
    assert control.receive(packet(deadlineMs=999))
    await asyncio.wait_for(control.close(), timeout=0.2)
    control.shutdown.assert_called_once()
    control.session.output.set_audio_enabled.assert_called_once_with(False)


async def test_current_deadline_bounds_disconnect(control):
    async def hang():
        await asyncio.Event().wait()
    control.disconnect = AsyncMock(side_effect=hang)
    assert control.receive(packet(deadlineMs=1010))
    await asyncio.wait_for(control.close(), timeout=0.2)
    control.shutdown.assert_called_once()


async def test_takeover_while_session_is_starting_still_disconnects(control):
    control.session.interrupt.side_effect = RuntimeError("not started")
    assert control.receive(packet())
    await control.close()
    control.shutdown.assert_called_once()


def test_failed_transfer_resumes_once_and_stale_failure_never_speaks(control):
    assert control.receive(packet("transfer_failed"))
    control.session.input.set_audio_enabled.assert_called_once_with(True)
    control.session.say.assert_called_once_with("May I take a message?", allow_interruptions=True)
    assert not control.receive(packet("transfer_failed"))
    assert control.receive(packet("transfer_failed", commandId="old-failure", deadlineMs=500))
    assert control.session.say.call_count == 1
    assert not control.stopping


def test_dedup_memory_bounded(control):
    for index in range(300):
        control.receive(packet("transfer_failed", commandId=f"failure:{index}", deadlineMs=1))
    assert len(control._seen) == 256


async def test_transfer_interrupts_then_plays_profile_statement_once(control):
    control.transfer_statement = "I will connect you now."
    control.session.interrupt = AsyncMock()
    speech = Mock(wait_for_playout=AsyncMock())
    control.session.say.return_value = speech
    assert control.receive(packet("transfer_requested"))
    assert control.transferring
    control.session.input.set_audio_enabled.assert_called_once_with(False)
    assert not control.receive(packet("transfer_requested"))
    await control._announcement_task
    control.session.interrupt.assert_awaited_once_with(force=True)
    control.session.say.assert_called_once_with("I will connect you now.", allow_interruptions=False)
    speech.wait_for_playout.assert_awaited_once()
    assert control.session.output.set_audio_enabled.call_args.args == (False,)
    control.disconnect.assert_not_awaited()
    assert not control.stopping


async def test_announcement_deadline_interrupts_long_speech_and_holds_for_human(control):
    control.transfer_statement = "Connecting."
    control.session.interrupt = AsyncMock()
    speech = Mock(wait_for_playout=AsyncMock())

    async def hang():
        await asyncio.Event().wait()

    speech.wait_for_playout.side_effect = hang
    control.session.say.return_value = speech
    assert control.receive(packet("transfer_requested", deadlineMs=1010))
    await asyncio.wait_for(control._announcement_task, timeout=0.2)
    speech.interrupt.assert_called_once_with(force=True)
    assert control.session.output.set_audio_enabled.call_args.args == (False,)
    assert control.session.input.set_audio_enabled.call_args.args == (False,)
    assert control.transferring
    assert not control.stopping


async def test_failure_cancels_outro_without_muting_resumed_screening(control):
    control.transfer_statement = "Connecting."
    control.session.interrupt.return_value = asyncio.get_running_loop().create_future()
    speech_started = asyncio.Event()

    async def playout():
        speech_started.set()
        await asyncio.Event().wait()

    speech = Mock(wait_for_playout=AsyncMock(side_effect=playout))
    control.session.say.return_value = speech
    control.receive(packet("transfer_requested"))
    control.session.interrupt.return_value.set_result(None)
    await speech_started.wait()
    announcement = control._announcement_task
    assert control.receive(packet('transfer_failed', commandId="result:2"))
    await asyncio.gather(announcement, return_exceptions=True)
    await control.close()
    speech.interrupt.assert_called_once_with(force=True)
    assert control.session.output.set_audio_enabled.call_args.args == (True,)
    assert control.session.input.set_audio_enabled.call_args.args == (True,)
    assert not control.stopping


async def test_answer_preserves_outro_until_playout_then_disconnects(control):
    control.transfer_statement = "Connecting you now."
    interrupted = asyncio.get_running_loop().create_future()
    interrupted.set_result(None)
    control.session.interrupt.return_value = interrupted
    started, finished = asyncio.Event(), asyncio.Event()

    async def playout():
        started.set()
        await finished.wait()

    speech = Mock(wait_for_playout=AsyncMock(side_effect=playout))
    control.session.say.return_value = speech
    assert control.receive(packet('transfer_requested'))
    await started.wait()
    assert control.receive(packet('human_answered', commandId='answer:2'))
    await asyncio.sleep(0)
    assert control.stopping
    assert control.session.output.set_audio_enabled.call_args.args == (True,)
    assert control.session.input.set_audio_enabled.call_args.args == (False,)
    control.session.shutdown.assert_not_called()
    control.disconnect.assert_not_awaited()
    speech.interrupt.assert_not_called()
    assert not control.receive(packet('transfer_failed', commandId='late-failure'))
    finished.set()
    await asyncio.wait_for(control._stop_task, timeout=0.2)
    control.disconnect.assert_awaited_once()
    control.shutdown.assert_called_once()
    assert control.session.say.call_count == 1
    assert control.session.output.set_audio_enabled.call_args.args == (False,)


async def test_answer_grace_extends_running_outro_but_still_bounds_hung_playout(control):
    control.transfer_statement = "Connecting."
    interrupted = asyncio.get_running_loop().create_future()
    interrupted.set_result(None)
    control.session.interrupt.return_value = interrupted
    started = asyncio.Event()

    async def playout():
        started.set()
        await asyncio.Event().wait()

    control.session.say.return_value = Mock(wait_for_playout=AsyncMock(side_effect=playout))
    control.receive(packet('transfer_requested', deadlineMs=1020))
    await started.wait()
    control.receive(packet('human_answered', commandId='answer:2', deadlineMs=1080))
    await asyncio.sleep(0.04)
    control.disconnect.assert_not_awaited()
    assert control.session.output.set_audio_enabled.call_args.args == (True,)
    await asyncio.wait_for(control._stop_task, timeout=0.2)
    control.disconnect.assert_awaited_once()
    assert control.session.output.set_audio_enabled.call_args.args == (False,)


async def test_answer_without_request_speaks_outro_once(control):
    control.transfer_statement = "Connecting."
    interrupted = asyncio.get_running_loop().create_future()
    interrupted.set_result(None)
    control.session.interrupt.return_value = interrupted
    control.session.say.return_value = Mock(wait_for_playout=AsyncMock())
    assert control.receive(packet('human_answered'))
    assert not control.receive(packet('transfer_requested', commandId='late-request'))
    await asyncio.wait_for(control._stop_task, timeout=0.2)
    control.session.say.assert_called_once_with('Connecting.', allow_interruptions=False)
    control.disconnect.assert_awaited_once()


async def test_answer_does_not_repeat_a_statement_that_already_finished(control):
    control.transfer_statement = "Connecting."
    interrupted = asyncio.get_running_loop().create_future()
    interrupted.set_result(None)
    control.session.interrupt.return_value = interrupted
    control.session.say.return_value = Mock(wait_for_playout=AsyncMock())
    control.receive(packet('transfer_requested'))
    await control._announcement_task
    control.receive(packet('human_answered', commandId='answer:2'))
    await control.close()
    control.session.say.assert_called_once_with('Connecting.', allow_interruptions=False)
    control.disconnect.assert_awaited_once()


def test_expired_transfer_request_does_not_interrupt_or_speak(control):
    assert control.receive(packet("transfer_requested", deadlineMs=999))
    control.session.interrupt.assert_not_called()
    control.session.say.assert_not_called()
    assert not control.transferring


async def test_startup_does_not_extend_transfer_deadline(control):
    control.ready = False
    assert control.receive(packet("transfer_requested", deadlineMs=2000))
    control.now_ms = lambda: 2001
    assert not control.activate()
    await control._announcement_task
    control.session.say.assert_not_called()
    assert control.session.output.set_audio_enabled.call_args.args == (False,)


async def test_announcement_provider_failure_keeps_agent_silent_until_transfer_result(control):
    control.transfer_statement = "Connecting."
    control.session.interrupt = AsyncMock()
    control.session.say.side_effect = ConnectionError("provider unavailable")
    assert control.receive(packet("transfer_requested"))
    await control._announcement_task
    assert control.session.output.set_audio_enabled.call_args.args == (False,)
    assert control.session.input.set_audio_enabled.call_args.args == (False,)
    assert not control.stopping
