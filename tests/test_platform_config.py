import io
import json
import urllib.error

import pytest

from aida_agent import platform_config
from aida_agent.platform_config import SETTING_KEYS, SettingsUnavailable, apply, fetch, resolve


def row(app, key, value):
    return {"app": app, "settingKey": key, "settingValue": value}


def test_later_scope_wins_blank_is_unset_and_other_keys_are_ignored():
    values = resolve([
        row("*", "LIVEKIT_URL", "wss://global"), row("aida", "LIVEKIT_URL", "wss://voice"),
        row("aida-agent", "AIDA_LLM_MODEL", " model/x "), row("aida", "AIDA_TTS_VOICE", "   "),
        row("officepulse", "AIDA_STT_MODEL", "not-mine"), row("*", "PARENT_DOMAIN", "x.tld"),
        row("aida", "OFFICEPULSE_API_BASE_URL", None),
    ])
    assert values == {"LIVEKIT_URL": "wss://voice", "AIDA_LLM_MODEL": "model/x"}


def test_duplicate_scoped_key_is_a_configuration_error():
    with pytest.raises(SettingsUnavailable, match="aida/LIVEKIT_URL"):
        resolve([row("aida", "LIVEKIT_URL", "a"), row("aida", "LIVEKIT_URL", "b")])


def test_apply_makes_the_store_the_only_source(monkeypatch):
    monkeypatch.setattr(platform_config, "fetch", lambda env: {"LIVEKIT_URL": "wss://store"})
    environ = {"NOCODB_BASE_URL": "http://n", "NOCODB_API_TOKEN": "t",
               "LIVEKIT_URL": "wss://pinned", "AIDA_LLM_MODEL": "pinned/model", "TZ": "UTC"}
    assert apply(environ) == ["LIVEKIT_URL"]
    assert environ == {"NOCODB_BASE_URL": "http://n", "NOCODB_API_TOKEN": "t",
                       "LIVEKIT_URL": "wss://store", "TZ": "UTC"}


def test_fetch_requires_bootstrap_without_naming_values():
    with pytest.raises(SettingsUnavailable, match="NOCODB_BASE_URL and NOCODB_API_TOKEN"):
        fetch({"NOCODB_BASE_URL": "http://nocodb", "NOCODB_API_TOKEN": " "})


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def fake_nocodb(monkeypatch, pages, token="secret-token"):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        assert request.get_header("Xc-token") == token
        assert timeout == platform_config.TIMEOUT_SECONDS
        path = request.full_url.split("http://nocodb", 1)[1]
        if path == "/api/v2/meta/bases":
            body = {"list": [{"id": "b1", "title": "PlatformConfig"}, {"id": "b2", "title": "Other"}]}
        elif path == "/api/v2/meta/bases/b1/tables":
            body = {"list": [{"id": "t1", "title": "cfg_tbl_Setting"}]}
        elif path.startswith("/api/v2/tables/t1/records?"):
            offset = int(path.rsplit("offset=", 1)[1])
            body = pages[offset // platform_config.PAGE_SIZE]
        else:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        return FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(platform_config.urllib.request, "urlopen", urlopen)
    return calls


def test_fetch_pages_through_the_table_found_by_name(monkeypatch):
    first = {"list": [row("aida", "LIVEKIT_URL", "wss://voice")] * 1
             + [row("ignored", "x", "y")] * (platform_config.PAGE_SIZE - 1),
             "pageInfo": {"isLastPage": False}}
    second = {"list": [row("aida-agent", "AIDA_TTS_VOICE", "asteria")], "pageInfo": {"isLastPage": True}}
    calls = fake_nocodb(monkeypatch, [first, second])
    values = fetch({"NOCODB_BASE_URL": "http://nocodb/", "NOCODB_API_TOKEN": "secret-token"})
    assert values == {"LIVEKIT_URL": "wss://voice", "AIDA_TTS_VOICE": "asteria"}
    assert [c.rsplit("/", 1)[1][:16] for c in calls] == ["bases", "tables", "records?limit=20",
                                                        "records?limit=20"]


def test_http_failures_name_the_status_never_the_token(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(platform_config.urllib.request, "urlopen", urlopen)
    with pytest.raises(SettingsUnavailable) as error:
        fetch({"NOCODB_BASE_URL": "http://nocodb", "NOCODB_API_TOKEN": "secret-token"})
    assert "401" in str(error.value) and "secret-token" not in str(error.value)


def test_setting_keys_cover_the_worker_and_preview_settings():
    from aida_agent.preview import VOICE_SETTINGS

    assert set(VOICE_SETTINGS) <= set(SETTING_KEYS)
    assert "LIVEKIT_AGENT_NAME" in SETTING_KEYS
