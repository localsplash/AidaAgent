"""Call-scoped authorization. OfficePulse owns token consumption and profile storage."""

import json
from dataclasses import dataclass

import aiohttp

from .config import (
    BootstrapConfiguration, CallConfiguration, DispatchConfiguration,
    InvalidConfiguration, MAX_PROFILE_BYTES, credential, strict_json,
)

MAX_RESPONSE_BYTES = MAX_PROFILE_BYTES + 4096


@dataclass(frozen=True, repr=False)
class SipLeg:
    identity: str
    sid: str
    route_token: str

    def __post_init__(self):
        for value in (self.identity, self.sid):
            if (not isinstance(value, str) or not value or len(value) > 256
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise InvalidConfiguration("invalid SIP participant")
        credential(self.route_token)


def authorized_profile(raw: bytes, dispatch: DispatchConfiguration,
                       room_name: str, leg: SipLeg) -> CallConfiguration:
    data = strict_json(raw, MAX_RESPONSE_BYTES)
    if set(data) != {"callSessionId", "roomName", "sipParticipantIdentity",
                     "sipParticipantSid", "profileSnapshot"}:
        raise InvalidConfiguration("invalid bootstrap response")
    if (data["callSessionId"] != dispatch.call_id or data["roomName"] != room_name
            or data["sipParticipantIdentity"] != leg.identity
            or data["sipParticipantSid"] != leg.sid):
        raise InvalidConfiguration("bootstrap binding mismatch")
    call = CallConfiguration.parse(json.dumps(data["profileSnapshot"]), room_name)
    # Routing scope is {pbxInstanceId, context}: the same context name on another PBX
    # instance is a different scope, so both must equal the trusted dispatch exactly.
    if call.pbx_instance_id != dispatch.pbx_instance_id or call.context != dispatch.context:
        raise InvalidConfiguration("bootstrap scope mismatch")
    return call


class BootstrapClient:
    def __init__(self, config: BootstrapConfiguration):
        self.config = config

    async def authorize(self, dispatch: DispatchConfiguration,
                        room_name: str, leg: SipLeg) -> CallConfiguration:
        """One attempt: an ambiguous response must never replay consumed credentials."""
        url = f"{self.config.base_url}/v1/agent/calls/{dispatch.call_id}/bootstrap"
        body = {
            "roomName": room_name, "sipParticipantIdentity": leg.identity,
            "sipParticipantSid": leg.sid, "routeToken": leg.route_token,
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=min(10, self.config.timeout_seconds), ceil_threshold=float("inf"),
                ),
                trust_env=False, auto_decompress=False,
            ) as client:
                async with client.post(
                    url, json=body, allow_redirects=False,
                    headers={"Authorization": f"Bearer {dispatch.bootstrap_token}",
                             "Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    if (response.status != 200 or response.content_type != "application/json"
                            or response.headers.get("Content-Encoding", "identity") != "identity"):
                        raise InvalidConfiguration("bootstrap authorization failed")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise InvalidConfiguration("bootstrap response too large")
                    return authorized_profile(bytes(raw), dispatch, room_name, leg)
        except (aiohttp.ClientError, OSError, TimeoutError):
            # URL, tokens, bodies, and upstream exception details never reach logs.
            raise InvalidConfiguration("bootstrap service unavailable") from None
