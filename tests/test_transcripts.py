import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from aida_agent.transcripts import TranscriptPublisher, TranscriptStream
from conftest import CALL_ID


def test_partials_replace_one_segment_and_final_starts_the_next():
    stream = TranscriptStream(CALL_ID)
    events = [stream.caller("Can", False), stream.caller("Can you help", False),
              stream.caller("Can you help me?", True), stream.caller("Tomorrow", True)]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert len({event["eventId"] for event in events}) == 4
    assert len({event["segmentId"] for event in events[:3]}) == 1
    assert events[3]["segmentId"] != events[2]["segmentId"]
    assert events[2]["isFinal"] is True
    # Mirror the handset's contract semantics, not any internal reducer function.
    display = {event["segmentId"]: event["text"] for event in events}
    assert list(display.values()) == ["Can you help me?", "Tomorrow"]


def test_assistant_envelope_and_reconnect_stream():
    first = TranscriptStream(CALL_ID)
    caller = first.caller("Hello", True)
    agent = first.assistant("How can I help?", "sdk-item-id")
    assert agent["speaker"] == "assistant"
    assert caller["speaker"] == "caller"
    assert agent["sequence"] == 2
    assert agent["callId"] == CALL_ID
    assert agent["type"] == "transcript"
    assert agent["isFinal"] is True
    UUID(agent["eventId"])
    UUID(agent["streamId"])
    datetime.fromisoformat(agent["timestamp"].replace("Z", "+00:00"))
    restarted = TranscriptStream(CALL_ID).caller("Again", True)
    assert restarted["streamId"] != agent["streamId"]
    assert restarted["sequence"] == 1


def test_empty_final_closes_the_existing_partial():
    stream = TranscriptStream(CALL_ID)
    partial = stream.caller("Can you help?", False)
    final = stream.caller("", True)
    assert final["segmentId"] == partial["segmentId"]
    assert final["text"] == "Can you help?"
    assert final["isFinal"] is True
    assert stream.caller("", True) is None


def test_empty_and_large_transcripts_stay_within_handset_and_packet_limits():
    stream = TranscriptStream(CALL_ID)
    assert stream.caller("", False) is None
    for text in ("😀" * 10000, "\\\n" * 10000, "\x01" * 10000 + "hello"):
        event = stream.caller(text, True)
        assert len(event["text"]) <= 8192
        assert len(json.dumps(event, ensure_ascii=False).encode()) < 15000


async def test_reliable_publisher_preserves_event_order():
    participant = Mock(publish_data=AsyncMock())
    failed = Mock()
    publisher = TranscriptPublisher(participant, failed)
    stream = TranscriptStream(CALL_ID)
    publisher.start()
    for text in ("first", "second", "third"):
        publisher.enqueue(stream.caller(text, True))
    await asyncio.wait_for(publisher._queue.join(), timeout=1)
    await publisher.close()
    assert [json.loads(args.args[0])["sequence"] for args in
            participant.publish_data.await_args_list] == [1, 2, 3]
    assert all(args.kwargs == {"topic": "transcript", "reliable": True}
               for args in participant.publish_data.await_args_list)
    failed.assert_not_called()


async def test_delivery_failure_and_overload_fail_closed_without_logging_content(caplog):
    participant = Mock(publish_data=AsyncMock(side_effect=RuntimeError("secret transcript")))
    failed = Mock()
    publisher = TranscriptPublisher(participant, failed)
    publisher.start()
    publisher.enqueue(TranscriptStream(CALL_ID).caller("secret transcript", True))
    await asyncio.wait_for(publisher._queue.join(), timeout=1)
    await publisher.close()
    failed.assert_called_once()
    assert "secret transcript" not in caplog.text
    overflow = TranscriptPublisher(participant, failed, capacity=1)
    overflow.enqueue({"sequence": 1})
    overflow.enqueue({"sequence": 2})
    overflow.enqueue({"sequence": 3})
    assert failed.call_count == 2
    await overflow.close()
