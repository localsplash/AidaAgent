"""Immutable installed artifact identity; no Git or runtime version overrides."""

import json
from importlib.resources import files

_path = files("aida_agent").joinpath("build-info.json")
BUILD_INFO = json.loads(_path.read_text(encoding="utf-8")) if _path.is_file() else {
    "version": "unbuilt", "revision": None, "sourceUpdatedAt": None,
    "timeZone": "America/Los_Angeles", "dirty": None,
}
