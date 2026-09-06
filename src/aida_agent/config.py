"""Validated dispatch v1. Model selection and credentials never come from a call."""

import json
import re
from dataclasses import dataclass, field
from typing import Mapping
from uuid import UUID

MAX_METADATA_BYTES = 16_384
MAX_SAFE_INTEGER = 9_007_199_254_740_991
REQUIRED = {"callSessionId", "tenantId", "businessName", "prompt", "locale", "didE164"}
OPTIONAL = {
    "schemaVersion", "tone", "objective", "openingStatement", "transferStatement",
    "failedTransferStatement",
}


class InvalidConfiguration(ValueError):
    """Messages deliberately contain no configuration values or field names from input."""


def strict_json(raw: str | bytes, max_bytes: int) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidConfiguration("duplicate JSON field")
            result[key] = value
        return result

    try:
        if not isinstance(raw, (str, bytes)) or len(
            raw.encode("utf-8") if isinstance(raw, str) else raw
        ) > max_bytes:
            raise InvalidConfiguration("invalid JSON size")
        value = json.loads(raw, object_pairs_hook=object_pairs)
        if not isinstance(value, dict):
            raise InvalidConfiguration("JSON object required")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidConfiguration("invalid JSON object") from None


def canonical_uuid(value: object) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, AttributeError):
        raise InvalidConfiguration("canonical UUID required") from None


def tenant_id(value: object) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", value):
        raise InvalidConfiguration("positive canonical tenant ID required")
    if int(value) > MAX_SAFE_INTEGER:
        raise InvalidConfiguration("tenant ID outside supported range")
    return value


@dataclass(frozen=True, repr=False)
class CallConfiguration:
    call_id: str
    tenant_id: str
    business_name: str
    prompt: str
    locale: str
    did_e164: str
    tone: str = ""
    objective: str = ""
    opening_statement: str = ""
    transfer_statement: str = ""
    failed_transfer_statement: str = ""
    schema_version: int = 1

    @classmethod
    def parse(cls, raw: str, room_name: str) -> "CallConfiguration":
        data = strict_json(raw, MAX_METADATA_BYTES)
        if not REQUIRED <= data.keys() or data.keys() - REQUIRED - OPTIONAL:
            raise InvalidConfiguration("dispatch fields do not match v1 allowlist")
        version = data.get("schemaVersion", 1)
        if type(version) is not int or version != 1:
            raise InvalidConfiguration("unsupported dispatch schema version")
        call_id = canonical_uuid(data["callSessionId"])
        if room_name != f"aida-{call_id}":
            raise InvalidConfiguration("dispatch room does not match call")

        def string(key, limit=2048, required=False):
            value = data.get(key, "")
            if not isinstance(value, str) or len(value) > limit or "\x00" in value:
                raise InvalidConfiguration("invalid dispatch text field")
            if required and not value.strip():
                raise InvalidConfiguration("required dispatch text is empty")
            return value

        locale = string("locale", 35, True)
        if not re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*", locale):
            raise InvalidConfiguration("invalid locale")
        did = string("didE164", 16, True)
        if not re.fullmatch(r"\+[1-9][0-9]{1,14}", did):
            raise InvalidConfiguration("invalid E.164 number")
        return cls(
            call_id=call_id, tenant_id=tenant_id(data["tenantId"]),
            business_name=string("businessName", 256, True),
            prompt=string("prompt", 12_000, True), locale=locale, did_e164=did,
            tone=string("tone", 256), objective=string("objective"),
            opening_statement=string("openingStatement"),
            transfer_statement=string("transferStatement"),
            failed_transfer_statement=string("failedTransferStatement"),
        )

    def instructions(self) -> str:
        context = {
            "businessName": self.business_name, "locale": self.locale,
            "tone": self.tone, "objective": self.objective,
        }
        return (
            "You are Aida, an office call-screening assistant. Be concise, helpful, and "
            "honest about being an automated assistant. Never claim that you transferred "
            "or ended a call: OfficePulse controls those actions. Ask only for information "
            "needed to help this business. Do not request passwords or payment secrets.\n"
            f"Business context: {json.dumps(context, ensure_ascii=False)}\n"
            f"Business instructions:\n{self.prompt}"
        )


@dataclass(frozen=True)
class DeploymentConfiguration:
    stt_model: str
    llm_model: str
    tts_model: str
    tts_voice: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "DeploymentConfiguration":
        def setting(name):
            value = env.get(name, "").strip()
            if not value or len(value) > 256 or any(c.isspace() for c in value):
                raise InvalidConfiguration(f"deployment setting {name} is required")
            return value

        return cls(
            stt_model=setting("AIDA_STT_MODEL"), llm_model=setting("AIDA_LLM_MODEL"),
            tts_model=setting("AIDA_TTS_MODEL"), tts_voice=setting("AIDA_TTS_VOICE"),
        )
