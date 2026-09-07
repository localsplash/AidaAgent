"""Ordered, bounded, reliable transcript delivery without logging call content."""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)
MAX_TEXT_BYTES = 6000  # At most 12 KB after JSON escaping plus the envelope.


class TranscriptStream:
    def __init__(self, call_id: str):
        self.call_id = call_id
        self.stream_id = str(uuid4())
        self.sequence = 0
        self._caller_segment: str | None = None
        self._caller_text = ""

    def _event(self, text: str, final: bool, segment: str, speaker: str) -> dict | None:
        if not isinstance(text, str) or not text.strip():
            return None
        # A single long utterance is truncated consistently in every revision.
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        text = text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
        self.sequence += 1
        return {
            "type": "transcript", "callId": self.call_id,
            "eventId": str(uuid4()), "streamId": self.stream_id,
            "sequence": self.sequence, "segmentId": segment,
            "text": text, "isFinal": final,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "speaker": speaker,
        }

    def caller(self, text: str, is_final: bool) -> dict | None:
        if not self._caller_segment:
            self._caller_segment = str(uuid4())
        if text.strip():
            self._caller_text = text
        elif is_final:
            # Close a displayed partial even if the provider's terminal event
            # contains no additional text.
            text = self._caller_text
        event = self._event(text, is_final, self._caller_segment, "caller")
        if is_final:
            self._caller_segment = None
            self._caller_text = ""
        return event

    def assistant(self, text: str, item_id: str) -> dict | None:
        return self._event(text, True, f"assistant-{item_id}", "assistant")


class TranscriptPublisher:
    """One sender preserves sequence. A bounded queue fails closed on backpressure."""

    def __init__(self, participant, on_failure, *, capacity: int = 128):
        self._participant = participant
        self._on_failure = on_failure
        self._queue = asyncio.Queue(maxsize=capacity)
        self._task: asyncio.Task | None = None
        self._closed = False

    def start(self):
        self._task = asyncio.create_task(self._run())

    def enqueue(self, event: dict | None):
        if event is None or self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._fail()

    def _fail(self):
        if not self._closed:
            self._closed = True
            logger.error("transcript delivery unavailable")
            self._on_failure()

    async def _run(self):
        while not self._closed:
            event = await self._queue.get()
            try:
                payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
                await asyncio.wait_for(
                    self._participant.publish_data(payload, reliable=True, topic="transcript"),
                    timeout=2,
                )
            except Exception:
                self._fail()
            finally:
                self._queue.task_done()

    async def close(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
