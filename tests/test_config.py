import json

import pytest

from aida_agent.config import (
    CallConfiguration, DeploymentConfiguration, InvalidConfiguration, MAX_PROFILE_BYTES,
)
from conftest import CALL_ID


def parse(value, room=f"aida-{CALL_ID}"):
    return CallConfiguration.parse(json.dumps(value), room)


def test_versioned_profile_contract(metadata):
    legacy = parse(metadata)
    assert legacy.tenant_id == "42"
    assert legacy.schema_version == 1
    del metadata["schemaVersion"]
    with pytest.raises(InvalidConfiguration):
        parse(metadata)
    assert "Ask how we can help." in legacy.instructions()
    assert legacy.opening_statement == "Thank you for calling."


@pytest.mark.parametrize("tenant", [1, "1", 9007199254740991, "9007199254740991"])
def test_safe_canonical_tenants(metadata, tenant):
    metadata["tenantId"] = tenant
    assert parse(metadata).tenant_id == str(tenant)


@pytest.mark.parametrize("tenant", [True, False, 0, -1, 1.5, 1.0, "01", "+1", "1.0", " 1",
                                    "1 ", "0", "", None, {}, [], "9007199254740992"])
def test_ambiguous_tenant_ids_rejected(metadata, tenant):
    metadata["tenantId"] = tenant
    with pytest.raises(InvalidConfiguration):
        parse(metadata)


@pytest.mark.parametrize("key,value", [
    ("apiKey", "top-secret"), ("model", "other/model"), ("stt", {"apiKey": "secret"}),
    ("voice", "expensive-voice"), ("LIVEKIT_API_SECRET", "private"),
    ("schemaVersion", 2), ("schemaVersion", True), ("schemaVersion", "1"),
    ("prompt", 123), ("prompt", ""), ("prompt", "x" * 12001), ("tone", {}),
    ("openingStatement", None), ("businessName", " "), ("locale", "en_US"),
    ("didE164", "5551234567"), ("callSessionId", "not-a-uuid"),
    ("callSessionId", CALL_ID.upper()),
])
def test_allowlist_and_types(metadata, key, value):
    metadata[key] = value
    with pytest.raises(InvalidConfiguration) as error:
        parse(metadata)
    assert "top-secret" not in str(error.value)
    assert "private" not in str(error.value)
    assert "expensive-voice" not in str(error.value)


def test_cross_call_room_rejected(metadata):
    with pytest.raises(InvalidConfiguration, match="room"):
        parse(metadata, "aida-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_missing_fields_rejected(metadata):
    del metadata["prompt"]
    with pytest.raises(InvalidConfiguration):
        parse(metadata)


@pytest.mark.parametrize("raw", ["[]", "null", "{", '{"tenantId":1,"tenantId":2}',
                                 "[" * 2000, "x" * (MAX_PROFILE_BYTES + 1)])
def test_invalid_serialized_metadata(raw):
    with pytest.raises(InvalidConfiguration):
        CallConfiguration.parse(raw, f"aida-{CALL_ID}")


def test_unicode_byte_budget_and_no_prompt_in_repr(metadata):
    metadata["prompt"] = "private-client-context" + "😀" * 12000
    with pytest.raises(InvalidConfiguration):
        parse(metadata)
    metadata["prompt"] = "private-client-context"
    assert "private-client-context" not in repr(parse(metadata))


def test_deployment_requires_explicit_models_and_ignores_extra_env():
    env = {"AIDA_STT_MODEL": "deepgram/nova-3-general", "AIDA_LLM_MODEL": "test/llm",
           "AIDA_TTS_MODEL": "cartesia/sonic-3", "AIDA_TTS_VOICE": "voice-id",
           "BUSINESS_PROMPT": "this cannot override call configuration"}
    config = DeploymentConfiguration.from_env(env)
    assert config.llm_model == "test/llm"
    for key in ("AIDA_STT_MODEL", "AIDA_LLM_MODEL", "AIDA_TTS_MODEL", "AIDA_TTS_VOICE"):
        with pytest.raises(InvalidConfiguration, match=key):
            DeploymentConfiguration.from_env({**env, key: ""})
