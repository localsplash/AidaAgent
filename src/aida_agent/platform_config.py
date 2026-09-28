"""Deployment settings from PlatformConfig, applied to the environment before the SDK starts.

The only bootstrap a deployment states is how to reach the store (NOCODB_BASE_URL and
NOCODB_API_TOKEN). Every setting in SETTING_KEYS comes from the `cfg_tbl_Setting` table of
the `PlatformConfig` base, resolved as app=* < app=aida < app=aida-agent. A same-named
environment variable is ignored: two homes for one credential meant a rotation in the store
could silently not take effect. Nothing here falls back to a guessed value.
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import MutableMapping
from typing import Mapping

BASE_NAME = "PlatformConfig"
TABLE_NAME = "cfg_tbl_Setting"
SCOPES = ("*", "aida", "aida-agent")
SETTING_KEYS = (
    # LiveKit project shared with OfficePulse (app=aida). The SDK reads these from the
    # environment, which is why the store is applied there rather than passed around.
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_AGENT_NAME",
    # OfficePulse's private API origin and the SIP trunk attribute (app=aida).
    "OFFICEPULSE_API_BASE_URL", "AIDA_ROUTE_TOKEN_ATTRIBUTE", "AIDA_BOOTSTRAP_TIMEOUT_SECONDS",
    # Model and voice choices (app=aida-agent).
    "AIDA_STT_MODEL", "AIDA_LLM_MODEL", "AIDA_TTS_MODEL", "AIDA_TTS_VOICE",
)
PAGE_SIZE = 200
TIMEOUT_SECONDS = 5


class SettingsUnavailable(RuntimeError):
    """Messages name endpoints and keys, never values or the API token."""


def _api(base_url: str, token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"{base_url}{path}", headers={"xc-token": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise SettingsUnavailable(f"NocoDB request failed ({error.code})") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise SettingsUnavailable("NocoDB could not be reached") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
        raise SettingsUnavailable("NocoDB returned an invalid record list")
    return payload


def _table_id(base_url: str, token: str) -> str:
    """Found by name at runtime, never by an ID from a config file."""
    bases = [b for b in _api(base_url, token, "/api/v2/meta/bases")["list"]
             if b.get("title") == BASE_NAME]
    if len(bases) != 1:
        raise SettingsUnavailable(f"Expected one NocoDB base named {BASE_NAME}, found {len(bases)}")
    tables = [t for t in _api(base_url, token, f"/api/v2/meta/bases/{bases[0]['id']}/tables")["list"]
              if t.get("title") == TABLE_NAME]
    if len(tables) != 1:
        raise SettingsUnavailable(
            f"Expected one {TABLE_NAME} table in {BASE_NAME}, found {len(tables)}")
    return tables[0]["id"]


def resolve(rows: list[dict]) -> dict[str, str]:
    """Scoped resolution: a later scope wins, blank rows are unset, duplicates are errors."""
    seen: set[tuple[str, str]] = set()
    for row in rows:
        app, key = row.get("app"), row.get("settingKey")
        if app not in SCOPES or not isinstance(key, str) or not key.strip():
            continue
        if (app, key) in seen:
            raise SettingsUnavailable(f"duplicate PlatformConfig setting {app}/{key}")
        seen.add((app, key))
    values: dict[str, str] = {}
    for scope in SCOPES:
        for row in rows:
            if row.get("app") != scope or row.get("settingKey") not in SETTING_KEYS:
                continue
            raw = row.get("settingValue")
            if raw is not None and str(raw).strip():
                values[row["settingKey"]] = str(raw).strip()
    return values


def fetch(env: Mapping[str, str]) -> dict[str, str]:
    base_url = env.get("NOCODB_BASE_URL", "").strip().rstrip("/")
    token = env.get("NOCODB_API_TOKEN", "").strip()
    if not base_url or not token:
        raise SettingsUnavailable("NOCODB_BASE_URL and NOCODB_API_TOKEN are required")
    table = _table_id(base_url, token)
    rows: list[dict] = []
    offset = 0
    while True:
        page = _api(base_url, token,
                    f"/api/v2/tables/{table}/records?limit={PAGE_SIZE}&offset={offset}")
        rows.extend(page["list"])
        if len(page["list"]) < PAGE_SIZE or (page.get("pageInfo") or {}).get("isLastPage") is True:
            break
        offset += PAGE_SIZE
    return resolve(rows)


def apply(environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Replace every SETTING_KEYS variable with the store's value; returns the keys now set."""
    values = fetch(environ)
    for key in SETTING_KEYS:
        environ.pop(key, None)
    environ.update(values)
    return sorted(values)
