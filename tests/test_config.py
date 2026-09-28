import json

import pytest

from aida_agent.config import (
    CallConfiguration, DeploymentConfiguration, InvalidConfiguration, MAX_PROFILE_BYTES,
    OPTIONAL, REQUIRED,
)
from conftest import CALL_ID, CONTEXT, PBX_INSTANCE_ID


def parse(value, room=f"aida-{CALL_ID}"):
    return CallConfiguration.parse(json.dumps(value), room)


def test_v2_key_sets_match_contract():
    # CONTRACT §5: exact v2 snapshot keys; nothing else is accepted.
    assert REQUIRED == {"schemaVersion", "callSessionId", "pbxInstanceId", "context",
                        "businessName", "prompt", "locale", "didE164"}
    assert OPTIONAL == {"tenantId", "tone", "objective", "openingStatement",
                        "transferStatement", "failedTransferStatement"}


def test_versioned_profile_contract(metadata):
    call = parse(metadata)
    assert call.schema_version == 2
    assert (call.pbx_instance_id, call.context) == (PBX_INSTANCE_ID, CONTEXT)
    assert call.tenant_id == "42"
    assert "Ask how we can help." in call.instructions()
    assert call.opening_statement == "Thank you for calling."
    del metadata["schemaVersion"]
    with pytest.raises(InvalidConfiguration):
        parse(metadata)


@pytest.mark.parametrize("version", [1, 3, "2", 2.0, True, None])
def test_only_schema_version_2_is_accepted(metadata, version):
    # v1 snapshots (no routing scope) are no longer accepted anywhere.
    metadata["schemaVersion"] = version
    with pytest.raises(InvalidConfiguration, match="schema version"):
        parse(metadata)


def test_v1_snapshot_shape_rejected(metadata):
    metadata["schemaVersion"] = 1
    del metadata["pbxInstanceId"], metadata["context"]
    with pytest.raises(InvalidConfiguration):
        parse(metadata)


@pytest.mark.parametrize("key", ["pbxInstanceId", "context", "schemaVersion", "prompt",
                                 "callSessionId", "businessName", "locale", "didE164"])
def test_missing_fields_rejected(metadata, key):
    del metadata[key]
    with pytest.raises(InvalidConfiguration):
        parse(metadata)


@pytest.mark.parametrize("key,value", [
    ("pbxInstanceId", "a"), ("pbxInstanceId", "A-b_c.9" * 10), ("pbxInstanceId", "x" * 80),
    ("context", "a"), ("context", "Office_1.x-y"), ("context", "x" * 40),
])
def test_scope_grammar_accepted(metadata, key, value):
    metadata[key] = value
    assert getattr(parse(metadata), {"pbxInstanceId": "pbx_instance_id",
                                     "context": "context"}[key]) == value


@pytest.mark.parametrize("key,value", [
    ("pbxInstanceId", ""), ("pbxInstanceId", "x" * 81), ("pbxInstanceId", "a/b"),
    ("pbxInstanceId", "a b"), ("pbxInstanceId", "ä"), ("pbxInstanceId", 1),
    ("pbxInstanceId", None), ("pbxInstanceId", ["officepulse-dev"]),
    ("context", ""), ("context", "x" * 41), ("context", "from carrier"), ("context", "a/b"),
    ("context", "x\n"), ("context", "ctx\x00"), ("context", 7), ("context", None),
    ("context", {"name": "example-office"}),
])
def test_scope_grammar_rejected(metadata, key, value):
    metadata[key] = value
    with pytest.raises(InvalidConfiguration) as error:
        parse(metadata)
    assert "officepulse" not in str(error.value)


def test_tenant_is_optional_customer_identity(metadata):
    del metadata["tenantId"]
    call = parse(metadata)
    assert call.tenant_id == ""
    assert (call.pbx_instance_id, call.context) == (PBX_INSTANCE_ID, CONTEXT)
    assert "tenant" not in call.instructions().lower()


@pytest.mark.parametrize("tenant", ["1", "42", "9007199254740991"])
def test_safe_canonical_tenants(metadata, tenant):
    metadata["tenantId"] = tenant
    assert parse(metadata).tenant_id == tenant


@pytest.mark.parametrize("tenant", [1, 42, 9007199254740991, True, False, 0, -1, 1.5, 1.0,
                                    "01", "+1", "1.0", " 1", "1 ", "0", "", None, {}, [],
                                    "9007199254740992"])
def test_non_canonical_tenant_ids_rejected(metadata, tenant):
    # Present-but-invalid is rejected; only absence means "no tenant identity".
    metadata["tenantId"] = tenant
    with pytest.raises(InvalidConfiguration):
        parse(metadata)


@pytest.mark.parametrize("key,value", [
    ("apiKey", "top-secret"), ("model", "other/model"), ("stt", {"apiKey": "secret"}),
    ("voice", "expensive-voice"), ("LIVEKIT_API_SECRET", "private"),
    ("iTenantId", 42), ("didContext", "from-bandwidth"), ("ingressContext", "from-bandwidth"),
    ("queue", "example-office.sales"), ("profileId", "p1"),
    ("schemaVersion", 1), ("schemaVersion", True), ("schemaVersion", "2"),
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
