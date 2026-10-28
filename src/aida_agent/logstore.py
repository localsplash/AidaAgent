"""LiveKitLogstore wiring: every SDK emission of this worker, stored in the Firebase bucket.

Off unless PlatformConfig says LOGSTORE_ENABLED=1 and holds the writer key as
LOGSTORE_KEY_JSON (app=aida-agent, secret). The library reads a key *file*, so the key
is written once to a private temporary file and only its path stays in the environment;
job processes inherit that path. Nothing here prints the key or ever stops the worker.
"""

import json
import logging
import os
import tempfile
from collections.abc import MutableMapping

logger = logging.getLogger("aida_agent")


def configure(environ: MutableMapping[str, str] = os.environ) -> bool:
    """Decide once, in the main process, whether logging is on; returns the decision."""
    key_json = environ.pop("LOGSTORE_KEY_JSON", "").strip()
    environ.pop("LOGSTORE_KEY_FILE", None)
    if environ.get("LOGSTORE_ENABLED", "").strip() != "1":
        environ["LOGSTORE_ENABLED"] = "0"
        return False
    environ["LOGSTORE_ENABLED"] = "0"
    if not key_json:
        logger.warning("LiveKitLogstore off: LOGSTORE_KEY_JSON is blank (PlatformConfig app=aida-agent)")
        return False
    try:
        key = json.loads(key_json)
    except ValueError:
        logger.warning("LiveKitLogstore off: LOGSTORE_KEY_JSON is not valid JSON")
        return False
    if not isinstance(key, dict) or key.get("type") != "service_account":
        logger.warning("LiveKitLogstore off: LOGSTORE_KEY_JSON is not a service-account key")
        return False
    fd, path = tempfile.mkstemp(prefix="logstore-key-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(key, file)
    os.chmod(path, 0o600)
    environ["LOGSTORE_KEY_FILE"] = path
    environ["LOGSTORE_ENABLED"] = "1"
    return True


def enabled(environ=os.environ) -> bool:
    return environ.get("LOGSTORE_ENABLED") == "1"


def watch_server(server) -> None:
    if enabled():
        from livekit_logstore import LiveKitLogstore

        LiveKitLogstore.watch_server(server)


class _Off:
    def watch(self, session):
        return self


def watch(ctx):
    """First call of a job; the result's watch(session) streams the session's events."""
    if not enabled():
        return _Off()
    from livekit_logstore import LiveKitLogstore

    return LiveKitLogstore.watch(ctx)


def stats() -> dict:
    """Counters of this process only: the worker's own files, not the job processes'."""
    if not enabled():
        return {"enabled": False}
    from livekit_logstore import LiveKitLogstore

    return {"enabled": True, **LiveKitLogstore.stats(), "scope": "worker_process"}
