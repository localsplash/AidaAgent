import json
import logging
import re
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from aida_agent import logstore, platform_config
from aida_agent.status import WorkerStatus

KEY = {"type": "service_account", "client_email": "writer@example.iam.gserviceaccount.com",
       "private_key": "-----BEGIN PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----\n",
       "token_uri": "https://oauth2.googleapis.com/token"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("LOGSTORE_ENABLED", "LOGSTORE_KEY_JSON", "LOGSTORE_KEY_FILE"):
        monkeypatch.delenv(key, raising=False)


def test_settings_come_from_platform_config():
    for key in ("LOGSTORE_ENABLED", "LOGSTORE_KEY_JSON", "LIVEKIT_TELEMETRY_ALLOW_PII"):
        assert key in platform_config.SETTING_KEYS
    values = platform_config.resolve([
        {"app": "aida-agent", "settingKey": "LOGSTORE_ENABLED", "settingValue": "1"},
        {"app": "aida-agent", "settingKey": "LOGSTORE_KEY_JSON", "settingValue": json.dumps(KEY)},
    ])
    assert values["LOGSTORE_ENABLED"] == "1" and json.loads(values["LOGSTORE_KEY_JSON"]) == KEY


@pytest.mark.parametrize("enabled", [None, "", "0", "true", " 1x"])
def test_off_unless_exactly_enabled(enabled):
    environ = {"LOGSTORE_KEY_JSON": json.dumps(KEY), "LOGSTORE_KEY_FILE": "/elsewhere"}
    if enabled is not None:
        environ["LOGSTORE_ENABLED"] = enabled
    assert logstore.configure(environ) is False
    assert environ == {"LOGSTORE_ENABLED": "0"}


@pytest.mark.parametrize("key_json", ["", "not json", "[]", json.dumps({**KEY, "type": "user"})])
def test_bad_or_missing_key_turns_it_off_without_printing_the_value(key_json, caplog, library):
    environ = {"LOGSTORE_ENABLED": "1", "LOGSTORE_KEY_JSON": key_json}
    with caplog.at_level(logging.WARNING, logger="aida_agent"):
        assert logstore.configure(environ) is False
    assert environ == {"LOGSTORE_ENABLED": "0"}
    assert "LiveKitLogstore off" in caplog.text
    assert "secret-material" not in caplog.text and "not json" not in caplog.text


def test_key_is_written_to_a_private_file_and_dropped_from_the_environment(caplog, library):
    environ = {"LOGSTORE_ENABLED": "1", "LOGSTORE_KEY_JSON": " " + json.dumps(KEY) + " "}
    with caplog.at_level(logging.DEBUG):
        assert logstore.configure(environ) is True
    path = Path(environ["LOGSTORE_KEY_FILE"])
    try:
        assert environ["LOGSTORE_ENABLED"] == "1" and "LOGSTORE_KEY_JSON" not in environ
        assert json.loads(path.read_text()) == KEY
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "secret-material" not in caplog.text
    finally:
        path.unlink()


def test_disabled_hooks_touch_nothing():
    server = SimpleNamespace(on=None)
    logstore.watch_server(server)
    job_log = logstore.watch(SimpleNamespace())
    assert job_log.watch(object()) is job_log
    assert WorkerStatus().snapshot()["logstore"] == {"enabled": False}


@pytest.fixture
def library(monkeypatch):
    """A stand-in for the installed library; CI builds without it."""
    calls = []

    class LiveKitLogstore:
        @classmethod
        def watch_server(cls, server):
            calls.append(("server", server))

        @classmethod
        def watch(cls, ctx):
            calls.append(("job", ctx))
            return "job-log"

        @classmethod
        def stats(cls):
            return {"records": 3, "uploads": 1, "failed": 0, "dropped": 0}

    module = ModuleType("livekit_logstore")
    module.LiveKitLogstore = LiveKitLogstore
    monkeypatch.setitem(sys.modules, "livekit_logstore", module)
    return calls


def test_enabled_hooks_call_the_library(monkeypatch, library):
    monkeypatch.setenv("LOGSTORE_ENABLED", "1")
    logstore.watch_server("srv")
    assert logstore.watch("ctx") == "job-log"
    assert library == [("server", "srv"), ("job", "ctx")]
    assert logstore.stats() == {"enabled": True, "records": 3, "uploads": 1, "failed": 0,
                                "dropped": 0, "scope": "worker_process"}


def test_image_without_the_library_stays_off(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "livekit_logstore", None)
    environ = {"LOGSTORE_ENABLED": "1", "LOGSTORE_KEY_JSON": json.dumps(KEY)}
    with caplog.at_level(logging.WARNING, logger="aida_agent"):
        assert logstore.configure(environ) is False
    assert environ == {"LOGSTORE_ENABLED": "0"}
    assert "not installed" in caplog.text


def test_pinned_tag_is_a_release_tag():
    ref = (Path(__file__).resolve().parents[1] / "LIVEKIT_LOGSTORE_REF").read_text().strip()
    assert re.fullmatch(r"v\d+\.\d+\.\d+", ref)
