"""Call-scoped, text-only session history and lk.transcription updates.

The SDK's generated SG_ ids are independent of ChatMessage ids. This adapter
assigns a segment once per caller turn and carries it into the committed history,
so a snapshot racing with partial/final delivery cannot duplicate an utterance.
The existing handset data-packet publisher is deliberately independent.
"""

import asyncio
import json
import logging
import time
from uuid import uuid4

from livekit import rtc

logger = logging.getLogger(__name__)
PAGE_CHARS = 6000  # Even fully escaped JSON stays below LiveKit's 15 KiB RPC limit.
MAX_SNAPSHOT_CHARS = 2 * 1024 * 1024


class TranscriptHistory:
    def __init__(self, room, session, call_id: str, available):
        self.room, self.session, self.call_id = room, session, call_id
        self.available = available
        self.sequence = 0
        self.committed = {}
        self.pending = None
        self.caller_finals = []
        self.snapshots = {}
        self.queue = asyncio.Queue(maxsize=128)
        self.task = None
        self.closed = False
        room.local_participant.register_rpc_method("get_transcript", self.get_transcript)

    def start(self):
        self.task = asyncio.create_task(self._publish())

    def _row(self, segment_id, role, text, final):
        self.sequence += 1
        return {"id": segment_id, "segment_id": segment_id, "role": role,
                "content": [text], "is_final": final, "sequence": self.sequence}

    def _enqueue(self, row):
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            # Observation must never end the telephone call. History remains authoritative.
            logger.warning("live transcript queue full; history catch-up remains available")

    def caller(self, text: str, final: bool):
        if self.closed or not text.strip():
            return
        segment = self.pending["segment_id"] if self.pending else f"caller-{uuid4()}"
        combined = " ".join([*self.caller_finals, text])
        self.pending = self._row(segment, "user", combined, False)
        if final:
            self.caller_finals.append(text)
        self._enqueue(self.pending)

    def item(self, item):
        if self.closed or item.role not in ("user", "assistant") or not item.text_content:
            return
        segment = (self.pending["segment_id"] if item.role == "user" and self.pending
                   else item.id)
        row = self._row(segment, item.role, item.text_content, True)
        row["id"] = item.id
        self.committed[item.id] = (segment, row["sequence"])
        if item.role == "user":
            self.pending = None
            self.caller_finals = []
        self._enqueue(row)

    def snapshot(self):
        items = []
        # Session history owns committed text, including interrupted assistant output.
        # Never send instructions, tool calls/results, audio, metrics or configuration.
        for item in self.session.history.items:
            if item.type != "message" or item.role not in ("user", "assistant"):
                continue
            text = item.text_content
            if not text:
                continue
            segment, sequence = self.committed.get(item.id, (item.id, 0))
            items.append({"id": item.id, "segment_id": segment, "role": item.role,
                          "content": [text], "is_final": True, "sequence": sequence})
        if self.pending:
            items.append(self.pending.copy())
        return {"callId": self.call_id, "items": items}

    async def get_transcript(self, data: rtc.RpcInvocationData) -> str:
        participant = self.room.remote_participants.get(data.caller_identity)
        if (self.closed or not self.available() or participant is None
                or not data.caller_identity.startswith("admin-observer-")
                or participant.attributes.get("aida.transcriptObserver") != self.room.name):
            raise rtc.RpcError(2001, "Transcript access unavailable")
        try:
            request = json.loads(data.payload or "{}")
            if not isinstance(request, dict):
                raise ValueError
            offset = request.get("offset", 0)
            if type(offset) is not int or offset < 0:
                raise ValueError
        except (ValueError, TypeError):
            raise rtc.RpcError(2002, "Invalid transcript request") from None
        now = time.monotonic()
        self.snapshots = {key: value for key, value in self.snapshots.items()
                          if now - value[0] < 60}
        if offset == 0:
            text = json.dumps(self.snapshot(), ensure_ascii=True, separators=(",", ":"))
            if len(text) > MAX_SNAPSHOT_CHARS:
                raise rtc.RpcError(2003, "Transcript exceeds history transfer limit")
            # One frozen snapshot per observer: subsequent pages cannot shift as turns arrive.
            if len(self.snapshots) >= 32 and data.caller_identity not in self.snapshots:
                raise rtc.RpcError(2004, "Transcript history busy; retry")
            token = str(uuid4())
            self.snapshots[data.caller_identity] = (now, token, text)
        snapshot = self.snapshots.get(data.caller_identity)
        if (snapshot is None or offset >= len(snapshot[2])
                or (offset and request.get("snapshotId") != snapshot[1])):
            raise rtc.RpcError(2002, "Transcript snapshot expired; retry")
        _, token, text = snapshot
        end = min(offset + PAGE_CHARS, len(text))
        result = json.dumps({"snapshotId": token, "text": text[offset:end],
                             "nextOffset": end if end < len(text) else None})
        if end == len(text):
            del self.snapshots[data.caller_identity]
        return result

    async def _publish(self):
        while not self.closed:
            row = await self.queue.get()
            try:
                await asyncio.wait_for(self.room.local_participant.send_text(
                    row["content"][0], topic="lk.transcription", attributes={
                        "lk.segment_id": row["segment_id"],
                        "lk.transcription_final": str(row["is_final"]).lower(),
                        "aida.call_id": self.call_id,
                        "aida.speaker": "caller" if row["role"] == "user" else "assistant",
                        "aida.sequence": str(row["sequence"]),
                    }), timeout=2)
            except Exception:
                logger.warning("live transcript delivery unavailable")
            finally:
                self.queue.task_done()

    async def close(self):
        self.closed = True
        self.room.local_participant.unregister_rpc_method("get_transcript")
        self.snapshots.clear()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
