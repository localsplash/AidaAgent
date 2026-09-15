import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from aida_agent.build_info import BUILD_INFO

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("utc, version, local", [
    ("2026-01-01T22:04:59+00:00", "2026.1.1.14.4", "2026-01-01T14:04:59-08:00"),
    ("2026-09-14T21:30:42+00:00", "2026.9.14.14.30", "2026-09-14T14:30:42-07:00"),
    ("2026-03-08T09:59:00+00:00", "2026.3.8.1.59", "2026-03-08T01:59:00-08:00"),
    ("2026-03-08T10:00:00+00:00", "2026.3.8.3.0", "2026-03-08T03:00:00-07:00"),
    ("2026-11-01T08:30:00+00:00", "2026.11.1.1.30", "2026-11-01T01:30:00-07:00"),
    ("2026-11-01T09:30:00+00:00", "2026.11.1.1.30", "2026-11-01T01:30:00-08:00"),
])
def test_pacific_stamp_is_stable_across_build_timezone(monkeypatch, utc, version, local):
    from datetime import datetime

    make_info = runpy.run_path(str(ROOT / "build_metadata.py"))["build_info"]
    monkeypatch.setenv("BUILD_REVISION", "a" * 40)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(int(datetime.fromisoformat(utc).timestamp())))
    monkeypatch.setenv("BUILD_DIRTY", "false")
    monkeypatch.setenv("TZ", "UTC")
    first = make_info()
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    assert make_info() == first
    assert first["version"] == version
    assert first["sourceUpdatedAt"] == local
    monkeypatch.setenv("BUILD_DIRTY", "true")
    assert make_info()["version"] == version + "-dirty"
    monkeypatch.setenv("BUILD_REVISION", "b" * 40)
    assert make_info()["revision"] != first["revision"]
    monkeypatch.delenv("BUILD_DIRTY")
    with pytest.raises(ValueError):
        make_info()
    monkeypatch.setenv("BUILD_DIRTY", "false")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "invalid")
    with pytest.raises(ValueError):
        make_info()


def test_reads_commit_time_and_detects_dirty_source(tmp_path, monkeypatch):
    import shutil

    shutil.copy(ROOT / "build_metadata.py", tmp_path)
    for key in ("BUILD_REVISION", "SOURCE_DATE_EPOCH", "BUILD_DIRTY"):
        monkeypatch.delenv(key, raising=False)
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-14T21:30:42+00:00",
           "GIT_COMMITTER_DATE": "2026-09-14T21:30:42+00:00"}

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, env=env, text=True)

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("add", ".")
    git("commit", "-m", "fixture")
    make_info = runpy.run_path(str(tmp_path / "build_metadata.py"))["build_info"]
    clean = make_info()
    assert clean["version"] == "2026.9.14.14.30"
    assert clean["revision"] == git("rev-parse", "HEAD").strip()
    assert clean["dirty"] is False
    (tmp_path / "new-source").write_text("changed")
    assert make_info()["dirty"] is True
    git("add", "new-source")
    assert make_info()["version"].endswith("-dirty")


def test_version_cli_has_no_voice_sdk_imports():
    result = subprocess.run([sys.executable, "-c", """
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith('livekit') or name == 'aida_agent.worker':
        raise AssertionError('version must not import voice SDK')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = ['aida-agent', 'version']
from aida_agent.cli import main
main()
"""], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == BUILD_INFO


@pytest.mark.parametrize("configured, expected", [("", "America/Los_Angeles"), ("UTC", "UTC")])
def test_cli_timezone_default_and_override(configured, expected):
    result = subprocess.run([sys.executable, "-c", """
import os, sys
sys.argv = ['aida-agent', 'version']
from aida_agent.cli import main
main()
print(os.environ['TZ'])
"""], env={**os.environ, "TZ": configured}, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines()[-1] == expected
