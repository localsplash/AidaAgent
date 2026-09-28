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
        if data["action"] not in ("transfer_requested", "human_answered", "transfer_failed"):
            raise InvalidConfiguration("unsupported control action")
        deadline = data["deadlineMs"]
        if type(deadline) is not int or not 0 < deadline <= MAX_SAFE_INTEGER:
            raise InvalidConfiguration("invalid control deadline")
        return cls(command_id, data["action"], deadline)


class ControlHandler:
    def __init__(self, call_id: str, session, disconnect, shutdown: Callable[[], None], *,
                 failed_statement: str = "", transfer_statement: str = "",
                 now_ms=None, ready: bool = True):
        self.call_id = call_id
        self.session = session
        self.disconnect = disconnect
        self.shutdown = shutdown
        self.failed_statement = failed_statement
        self.transfer_statement = transfer_statement
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.stopping = False
        self.transferring = False
        self._transfer_deadline_ms = 0
        self.ready = ready
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._stop_task: asyncio.Task | None = None
        self._announcement_task: asyncio.Task | None = None
        self._announcement_timeout: asyncio.Timeout | None = None

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
            # Stop ordinary turns, but keep the outro audible to both parties
            # until playout completes or the PBX's post-answer deadline expires.
            self.stopping = True
            remaining = max(0, min(10_000, command.deadline_ms - self.now_ms())) / 1000
            if self.ready and self.session is not None and remaining > 0:
                self._transfer_deadline_ms = self.now_ms() + int(remaining * 1000)
                if not self.transferring and self.transfer_statement:
                    # A missed request packet must not suppress the graceful exit.
                    self.transferring = True
                    self._start_announcement()
                elif (self._announcement_timeout is not None
                      and not self._announcement_timeout.expired()):
                    self._announcement_timeout.reschedule(
                        asyncio.get_running_loop().time() + remaining)
            if self.ready and remaining > 0 and self._announcement_task is not None:
                self.session.input.set_audio_enabled(False)
                self._stop_task = asyncio.create_task(
                    self._finish_handoff(self._transfer_deadline_ms))
            else:
                self._cancel_announcement()
                self._silence()
                self._stop_task = asyncio.create_task(self._stop(remaining))
        elif command.action == "transfer_requested":
            if command.deadline_ms > self.now_ms():
                self.transferring = True
                self._transfer_deadline_ms = command.deadline_ms
                self._start_announcement()
        elif command.deadline_ms >= self.now_ms():
            self.transferring = False
            self._cancel_announcement()
            if self.ready and self.session is not None:
                self.session.output.set_audio_enabled(True)
                self.session.input.set_audio_enabled(True)
                if self.failed_statement:
                    try:
                        self.session.say(self.failed_statement, allow_interruptions=True)
                    except RuntimeError:
                        # Normal opening begins once startup completes.
                        pass
        return True

    def activate(self) -> bool:
        """Enable normal turns unless a takeover arrived during authorized startup."""
        self.ready = True
        if self.transferring:
            self._start_announcement()
            return False
        return not self.stopping

    def _start_announcement(self):
        if not self.ready or self.session is None:
            return
        self._cancel_announcement()
        # No ordinary turns or MOH should compete with the profile statement.
        self.session.input.set_audio_enabled(False)
        self.session.output.set_audio_enabled(False)
        self._transfer_deadline_ms = min(self._transfer_deadline_ms, self.now_ms() + 10_000)
        self._announcement_task = asyncio.create_task(self._announce())

    def _cancel_announcement(self):
        if self._announcement_task:
            self._announcement_task.cancel()
            self._announcement_task = None
            self._announcement_timeout = None

    async def _announce(self):
        speech = None
        try:
            remaining = (self._transfer_deadline_ms - self.now_ms()) / 1000
            if remaining <= 0:
                return
            self._announcement_timeout = asyncio.timeout(remaining)
            async with self._announcement_timeout:
                # Await interruption before enqueueing the outro; otherwise a
                # pending response/interruption can swallow the new statement.
                await self.session.interrupt(force=True)
                if self.transfer_statement:
                    self.session.output.set_audio_enabled(True)
                    speech = self.session.say(self.transfer_statement, allow_interruptions=False)
                    await speech.wait_for_playout()
        except Exception:
            # TTS failure must not resume normal turns or prevent handoff.
            pass
        finally:
            if speech:
                speech.interrupt(force=True)
            # An obsolete task must not mute audio restored by transfer_failed.
            if self._announcement_task is asyncio.current_task():
                self.session.output.set_audio_enabled(False)
                self._announcement_task = None
                self._announcement_timeout = None

    def _silence(self):
        if self.session is not None:
            self.session.input.set_audio_enabled(False)
            self.session.output.set_audio_enabled(False)
            try:
                self.session.interrupt(force=True)
                self.session.shutdown(drain=False)
            except RuntimeError:
                pass

    async def _finish_handoff(self, deadline_ms: int):
        try:
            if self._announcement_task is not None:
                await asyncio.wait_for(asyncio.shield(self._announcement_task),
                                       timeout=max(0, (deadline_ms - self.now_ms()) / 1000))
        except (TimeoutError, asyncio.CancelledError):
            pass
        finally:
            self._cancel_announcement()
            self._silence()
        await self._stop(max(0, (deadline_ms - self.now_ms()) / 1000))

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
        announcement = self._announcement_task
        self._cancel_announcement()
        if announcement:
            await asyncio.gather(announcement, return_exceptions=True)
        if self._stop_task:
            await self._stop_task
