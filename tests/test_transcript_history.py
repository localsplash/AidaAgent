import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import rtc

from aida_agent.transcript_history import TranscriptHistory


def message(id="item-1", role="user", text="Hello there", type="message"):
    return SimpleNamespace(id=id, role=role, text_content=text, type=type)


@pytest.fixture
def transcript():
    observer = SimpleNamespace(attributes={"aida.transcriptObserver": "aida-call"})
    room = SimpleNamespace(name="aida-call", remote_participants={"admin-observer-1": observer},
                           local_participant=Mock(send_text=AsyncMock()))
    session = SimpleNamespace(history=SimpleNamespace(items=[]))
    history = TranscriptHistory(room, session, "call", lambda: True)
    return history


def request(payload="{}", identity="admin-observer-1"):
    return rtc.RpcInvocationData("request", identity, payload, 10)


async def read_snapshot(history, identity="admin-observer-1"):
    text = ""
    payload = "{}"
    while True:
        result = await history.get_transcript(request(payload, identity))
        assert len(result.encode()) < 15 * 1024
        page = json.loads(result)
        text += page["text"]
        if page["nextOffset"] is None:
            return json.loads(text)
        payload = json.dumps({"offset": page["nextOffset"], "snapshotId": page["snapshotId"]})


async def test_snapshot_and_native_stream_share_segment_across_partial_and_commit(transcript):
    transcript.start()
    transcript.caller("Hello", False)
    partial = (await read_snapshot(transcript))["items"][0]
    assert partial["is_final"] is False
    transcript.caller("Hello there", True)
    item = message()
    transcript.session.history.items.append(item)
    transcript.item(item)
    snapshot = await read_snapshot(transcript)
    assert len(snapshot["items"]) == 1
    final = snapshot["items"][0]
    assert final["id"] == item.id
    assert final["segment_id"] == partial["segment_id"]
    assert final["content"] == ["Hello there"]
    assert final["is_final"] is True
    await transcript.queue.join()
    calls = transcript.room.local_participant.send_text.call_args_list
    assert len(calls) == 3
    assert {c.kwargs["attributes"]["lk.segment_id"] for c in calls} == {partial["segment_id"]}
    assert calls[-1].kwargs["attributes"]["lk.transcription_final"] == "true"
    assert calls[-1].kwargs["topic"] == "lk.transcription"
    await transcript.close()
    transcript.room.local_participant.unregister_rpc_method.assert_called_once_with("get_transcript")


async def test_history_is_authoritative_and_excludes_instructions_tools_and_metrics(transcript):
    answer = message("answer", "assistant", "Actually spoken")
    transcript.item(answer)
    transcript.session.history.items = [
        message("instructions", "system", "private instructions"),
        message("tool", "assistant", "private tool payload", type="function_call"), answer,
    ]
    assert (await read_snapshot(transcript))["items"] == [{
        "id": "answer", "segment_id": "answer", "role": "assistant",
        "content": ["Actually spoken"], "is_final": True, "sequence": 1,
    }]
    await transcript.close()


async def test_long_unicode_history_pages_are_frozen_and_fit_rpc_limit(transcript):
    long_text = '"\\😀' * 10000
    transcript.session.history.items = [message(text=long_text)]
    first = json.loads(await transcript.get_transcript(request()))
    # A turn arriving between pages belongs to the next snapshot, not this one.
    transcript.session.history.items.append(message("second", text="new turn"))
    text = first["text"]
    page = first
    while page["nextOffset"] is not None:
        raw = await transcript.get_transcript(request(json.dumps({
            "offset": page["nextOffset"], "snapshotId": page["snapshotId"],
        })))
        assert len(raw.encode()) < 15 * 1024
        page = json.loads(raw)
        text += page["text"]
    assert len(json.loads(text)["items"]) == 1
    assert json.loads(text)["items"][0]["content"] == [long_text]
    assert len((await read_snapshot(transcript))["items"]) == 2
    assert not transcript.snapshots
    await transcript.close()


@pytest.mark.parametrize("identity,attributes", [
    ("sip-caller", {"aida.transcriptObserver": "aida-call"}),
    ("admin-observer-other", {"aida.transcriptObserver": "other-room"}),
    ("admin-observer-missing", {}),
])
async def test_history_rejects_unauthorized_room_participants(transcript, identity, attributes):
    transcript.room.remote_participants[identity] = SimpleNamespace(attributes=attributes)
    with pytest.raises(rtc.RpcError):
        await transcript.get_transcript(request(identity=identity))
    await transcript.close()


async def test_snapshot_cannot_be_reused_by_another_observer_or_after_close(transcript):
    transcript.session.history.items = [message(text="x" * 9000)]
    first = json.loads(await transcript.get_transcript(request()))
    transcript.room.remote_participants["admin-observer-2"] = SimpleNamespace(
        attributes={"aida.transcriptObserver": "aida-call"})
    with pytest.raises(rtc.RpcError):
        await transcript.get_transcript(request(json.dumps({
            "offset": first["nextOffset"], "snapshotId": first["snapshotId"],
        }), "admin-observer-2"))
    await transcript.close()
    with pytest.raises(rtc.RpcError):
        await transcript.get_transcript(request())


async def test_multiple_stt_finals_form_one_pending_turn_until_history_commit(transcript):
    transcript.caller("First sentence.", True)
    transcript.caller("And another", False)
    transcript.caller("And another sentence.", True)
    before = (await read_snapshot(transcript))["items"][0]
    assert before["content"] == ["First sentence. And another sentence."]
    item = message(text="First sentence. And another sentence.")
    transcript.session.history.items.append(item)
    transcript.item(item)
    after = (await read_snapshot(transcript))["items"][0]
    assert before["segment_id"] == after["segment_id"]
    await transcript.close()
