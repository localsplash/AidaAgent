"""Stamp wheels with source identity, never the wall clock of the build."""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


def build_info():
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    if os.environ.get("BUILD_REVISION") or os.environ.get("SOURCE_DATE_EPOCH"):
        revision = os.environ.get("BUILD_REVISION", "")
        epoch = os.environ.get("SOURCE_DATE_EPOCH", "")
        if os.environ.get("BUILD_DIRTY") not in ("true", "false"):
            raise ValueError("Explicit metadata requires BUILD_DIRTY=true or false")
        dirty = os.environ["BUILD_DIRTY"] == "true"
    else:
        revision = git("rev-parse", "HEAD")
        epoch = git("show", "-s", "--format=%ct", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision):
        raise ValueError("Build requires a full Git revision")
    if not re.fullmatch(r"[0-9]+", epoch):
        raise ValueError("Build requires integer SOURCE_DATE_EPOCH")
    zone = "America/Los_Angeles"
    date = datetime.fromtimestamp(int(epoch), ZoneInfo(zone))
    version = f"{date.year}.{date.month}.{date.day}.{date.hour}.{date.minute}"
    return {
        "version": version + ("-dirty" if dirty else ""), "revision": revision,
        "sourceUpdatedAt": date.isoformat(timespec="seconds"), "timeZone": zone,
        "dirty": dirty,
    }


if __name__ == "__main__":
    print(json.dumps(build_info()))
