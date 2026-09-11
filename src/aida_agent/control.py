"""Server-origin takeover commands; this worker never manages other participants."""

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from .config import InvalidConfiguration, MAX_SAFE_INTEGER, canonical_uuid, strict_json


@dataclass(frozen=True)
class ControlCommand:
    command_id: str
    action: str
    deadline_ms: int

    @classmethod
    def parse(cls, packet, call_id: str) -> "ControlCommand":
        # Server RoomService.SendData carries no participant. A participant identity
        # claiming to be OfficePulse, even an admin-looking identity, is untrusted.
        if packet.topic != "aida.control" or packet.participant is not None:
            raise InvalidConfiguration("untrusted control origin")
        data = strict_json(packet.data, 2048)
        if set(data) != {"type", "callId", "commandId", "action", "deadlineMs"}:
            raise InvalidConfiguration("invalid control fields")
        if data["type"] != "control" or canonical_uuid(data["callId"]) != call_id:
            raise InvalidConfiguration("control call mismatch")
        command_id = data["commandId"]
        if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", command_id):
            raise InvalidConfiguration("invalid command ID")
        if data["action"] not in ("human_answered", "transfer_failed"):
            raise InvalidConfiguration("unsupported control action")
        deadline = data["deadlineMs"]
        if type(deadline) is not int or not 0 < deadline <= MAX_SAFE_INTEGER:
            raise InvalidConfiguration("invalid control deadline")
        return cls(command_id, data["action"], deadline)


class ControlHandler:
    def __init__(self, call_id: str, session, disconnect, shutdown: Callable[[], None], *,
                 failed_statement: str = "", now_ms=None, ready: bool = True):
        self.call_id = call_id
        self.session = session
        self.disconnect = disconnect
        self.shutdown = shutdown
        self.failed_statement = failed_statement
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.stopping = False
        self.ready = ready
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._stop_task: asyncio.Task | None = None

    def receive(self, packet) -> bool:
        try:
            command = ControlCommand.parse(packet, self.call_id)
        except (InvalidConfiguration, AttributeError):
            return False
        if command.command_id in self._seen or self.stopping:
            return False
        self._seen[command.command_id] = None
        if len(self._seen) > 256:
            self._seen.popitem(last=False)
        if command.action == "human_answered":
            # Stop immediately, including expired commands. Never extend a takeover
            # deadline or wait for another model turn after the human is bridged.
            self.stopping = True
            if self.session is not None:
                self.session.input.set_audio_enabled(False)
                self.session.output.set_audio_enabled(False)
                try:
                    self.session.interrupt(force=True)
                    self.session.shutdown(drain=False)
                except RuntimeError:
                    # Startup guard prevents a greeting when start is still in progress.
                    pass
            remaining = max(0, min(10_000, command.deadline_ms - self.now_ms())) / 1000
            self._stop_task = asyncio.create_task(self._stop(remaining))
        elif self.ready and self.session is not None and command.deadline_ms >= self.now_ms():
            self.session.input.set_audio_enabled(True)
            if self.failed_statement:
                try:
                    self.session.say(self.failed_statement, allow_interruptions=True)
                except RuntimeError:
                    # Failure during startup needs no extra announcement: the
                    # normal opening and screening begin once startup completes.
                    pass
        return True

    async def _stop(self, remaining: float):
        try:
            # Only disconnect this JobContext's local room participant. No room
            # deletion, SIP participant removal, or telephony API exists here.
            await asyncio.wait_for(self.disconnect(), timeout=max(0.001, remaining))
        except Exception:
            pass
        finally:
            self.shutdown()

    async def close(self):
        if self._stop_task:
            await self._stop_task
