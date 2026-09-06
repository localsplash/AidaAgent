import json

import pytest

CALL_ID = "281c6b8e-6a61-45ba-9165-eb199825d12e"


@pytest.fixture
def metadata():
    return {
        "callSessionId": CALL_ID, "tenantId": "42", "businessName": "Example Office",
        "prompt": "Ask how we can help.", "locale": "en-US", "didE164": "+15551234567",
        "openingStatement": "Thank you for calling.",
        "failedTransferStatement": "No one is available. May I take a message?",
    }


@pytest.fixture
def call(metadata):
    from aida_agent.config import CallConfiguration
    return CallConfiguration.parse(json.dumps(metadata), f"aida-{CALL_ID}")
