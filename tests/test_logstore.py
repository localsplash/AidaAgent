import json
import logging
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

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
def test_bad_or_missing_key_turns_it_off_without_printing_the_value(key_json, caplog):
    environ = {"LOGSTORE_ENABLED": "1", "LOGSTORE_KEY_JSON": key_json}
    with caplog.at_level(logging.WARNING, logger="aida_agent"):
        assert logstore.configure(environ) is False
    assert environ == {"LOGSTORE_ENABLED": "0"}
    assert "LiveKitLogstore off" in caplog.text
    assert "secret-material" not in caplog.text and "not json" not in caplog.text


def test_key_is_written_to_a_private_file_and_dropped_from_the_environment(caplog):
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


def test_enabled_hooks_call_the_library(monkeypatch):
    import livekit_logstore

    calls = []
    monkeypatch.setenv("LOGSTORE_ENABLED", "1")
    monkeypatch.setattr(livekit_logstore.LiveKitLogstore, "watch_server",
                        classmethod(lambda cls, server: calls.append(("server", server))))
    monkeypatch.setattr(livekit_logstore.LiveKitLogstore, "watch",
                        classmethod(lambda cls, ctx: calls.append(("job", ctx)) or "job-log"))
    monkeypatch.setattr(livekit_logstore.LiveKitLogstore, "stats",
                        classmethod(lambda cls: {"records": 3, "uploads": 1, "failed": 0, "dropped": 0}))
    logstore.watch_server("srv")
    assert logstore.watch("ctx") == "job-log"
    assert calls == [("server", "srv"), ("job", "ctx")]
    assert logstore.stats() == {"enabled": True, "records": 3, "uploads": 1, "failed": 0,
                                "dropped": 0, "scope": "worker_process"}


def test_pinned_version_matches_the_checked_out_tag():
    root = Path(__file__).resolve().parents[1]
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    pinned = next(d.split("==")[1] for d in deps if d.startswith("livekit-logstore=="))
    assert (root / "LIVEKIT_LOGSTORE_REF").read_text().strip() == f"v{pinned}"
