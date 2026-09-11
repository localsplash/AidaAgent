"""Observe the actual SIP leg, including delayed attributes and participant changes."""

import asyncio

from livekit import rtc

from .bootstrap import SipLeg
from .config import InvalidConfiguration


class SipAdmission:
    def __init__(self, room, attribute: str, abort):
        self.room = room
        self.attribute = attribute
        self.abort = abort
        self.changed = asyncio.Event()
        self.leg: SipLeg | None = None
        self.closed = False
        self.handlers = {
            "participant_connected": self._changed,
            "participant_disconnected": self._disconnected,
            "participant_attributes_changed": self._changed,
            "disconnected": self._room_disconnected,
        }
        for name, handler in self.handlers.items():
            room.on(name, handler)

    def _participants(self):
        return [p for p in self.room.remote_participants.values()
                if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP]

    def _changed(self, *_args):
        self.changed.set()
        if self.leg is not None:
            try:
                self.validate()
            except InvalidConfiguration:
                self.abort()

    def _disconnected(self, participant):
        # Do not depend on when the SDK removes the participant from its map.
        if self.leg is not None and participant.sid == self.leg.sid:
            self.abort()
        self._changed()

    def _room_disconnected(self, *_args):
        self.abort()

    async def wait(self) -> SipLeg:
        while True:
            self.changed.clear()
            participants = self._participants()
            if len(participants) > 1:
                raise InvalidConfiguration("ambiguous SIP participants")
            if participants:
                participant = participants[0]
                token = participant.attributes.get(self.attribute)
                if token is not None:
                    self.leg = SipLeg(participant.identity, participant.sid, token)
                    return self.leg
            await self.changed.wait()

    def validate(self):
        participants = self._participants()
        if self.leg is None or len(participants) != 1:
            raise InvalidConfiguration("SIP participant changed")
        p = participants[0]
        if (p.identity != self.leg.identity or p.sid != self.leg.sid
                or p.attributes.get(self.attribute) != self.leg.route_token):
            raise InvalidConfiguration("SIP participant changed")

    def close(self):
        if not self.closed:
            self.closed = True
            for name, handler in self.handlers.items():
                self.room.off(name, handler)
