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
        self._audio_authorized = False
        self._audio_requested = {}
        self._audio_connected = False
        self.handlers = {
            "participant_connected": self._changed,
            "participant_disconnected": self._disconnected,
            "participant_attributes_changed": self._changed,
            "disconnected": self._room_disconnected,
            "track_published": self._track_published,
            "track_subscribed": self._track_subscribed,
            "track_subscription_failed": self._track_subscription_failed,
            "track_unsubscribed": self._track_unsubscribed,
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
        if self.closed or self.leg is None or len(participants) != 1:
            raise InvalidConfiguration("SIP participant changed")
        p = participants[0]
        if (p.identity != self.leg.identity or p.sid != self.leg.sid
                or p.attributes.get(self.attribute) != self.leg.route_token):
            raise InvalidConfiguration("SIP participant changed")

    def _bound_audio(self, publication, participant):
        return (self.leg is not None and participant.identity == self.leg.identity
                and participant.sid == self.leg.sid
                and publication.kind == rtc.TrackKind.KIND_AUDIO
                and publication.source == rtc.TrackSource.SOURCE_MICROPHONE)

    def _track_published(self, publication, participant):
        if self.closed or not self._audio_authorized:
            return
        try:
            self.validate()
            if (self._bound_audio(publication, participant)
                    and publication.sid not in self._audio_requested):
                self._audio_requested[publication.sid] = publication
                publication.set_subscribed(True)
        except Exception:
            self.abort()
        self.changed.set()

    def _track_subscribed(self, _track, publication, participant):
        if self._bound_audio(publication, participant):
            self.changed.set()

    def _track_subscription_failed(self, participant, track_sid, _error):
        if (not self.closed and self.leg is not None
                and participant.sid == self.leg.sid and track_sid in self._audio_requested):
            self.abort()

    def _track_unsubscribed(self, _track, publication, participant):
        if not self.closed and self._audio_connected and self._bound_audio(publication, participant):
            # A live session without its authorized input must not remain ready.
            self.abort()

    async def subscribe_audio(self):
        """Called only after bootstrap. RoomIO selects tracks but does not subscribe."""
        self.validate()
        self._audio_authorized = True
        while not self.closed:
            self.changed.clear()
            self.validate()
            participant = self.room.remote_participants[self.leg.identity]
            for publication in participant.track_publications.values():
                self._track_published(publication, participant)
                if (self._bound_audio(publication, participant)
                        and publication.track is not None):
                    self._audio_connected = True
                    return
            # The enclosing worker startup timeout also bounds late/missing audio.
            self.changed.clear()
            await self.changed.wait()
        raise InvalidConfiguration("SIP audio unavailable")

    def close(self):
        if not self.closed:
            self.closed = True
            for name, handler in self.handlers.items():
                self.room.off(name, handler)
            for publication in self._audio_requested.values():
                try:
                    publication.set_subscribed(False)
                except Exception:
                    pass
            self.changed.set()
