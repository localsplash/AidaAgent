import json
from pathlib import Path

import pytest

CALL_ID = "281c6b8e-6a61-45ba-9165-eb199825d12e"
PBX_INSTANCE_ID = "officepulse-dev"
CONTEXT = "example-office"


@pytest.fixture
def metadata():
    return {
        "schemaVersion": 2, "callSessionId": CALL_ID, "pbxInstanceId": PBX_INSTANCE_ID,
        "context": CONTEXT, "tenantId": "42", "businessName": "Example Office",
        "prompt": "Ask how we can help.", "locale": "en-US", "didE164": "+15551234567",
        "openingStatement": "Thank you for calling.",
        "failedTransferStatement": "No one is available. May I take a message?",
    }


@pytest.fixture
def call(metadata):
    from aida_agent.config import CallConfiguration
    return CallConfiguration.parse(json.dumps(metadata), f"aida-{CALL_ID}")


@pytest.fixture
def dispatch():
    return {"callSessionId": CALL_ID, "bootstrapToken": "b" * 43,
            "pbxInstanceId": PBX_INSTANCE_ID, "context": CONTEXT}


@pytest.fixture
def contract():
    # Shared cross-repository example (CONTRACT §5); byte-identical in OfficePulse.
    return json.loads((Path(__file__).parent / "fixtures/bootstrap-v2.json").read_text())
