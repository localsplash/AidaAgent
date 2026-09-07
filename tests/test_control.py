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
