"""Aggregate, process-local worker diagnostics. Never expose job IDs or credentials."""

import time
from datetime import datetime
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from aiohttp import web

from .build_info import BUILD_INFO


class WorkerStatus:
    def __init__(self):
        self.started_at = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")
        self.started = time.monotonic()
        self.connection_state = "starting"
        self.websocket = None
        self.connections = 0
        self.started_sessions = 0
        self.active_sessions = 0
        self.processed_sessions = 0
        self.failed_sessions = 0
        # Executors live in the parent worker and represent one dispatched job each.
        # Weak keys deduplicate callbacks without retaining every past session forever.
        self.jobs = WeakKeyDictionary()

    def connected(self, websocket):
        self.websocket = websocket
        self.connection_state = "connected"
        self.connections += 1

    def disconnected(self):
        self.websocket = None
        if self.connection_state not in ("stopping", "stopped", "failed"):
            self.connection_state = "reconnecting" if self.connections else "connecting"

    def observe_job(self, executor, state):
        if executor not in self.jobs:
            self.jobs[executor] = "running"
            self.started_sessions += 1
            self.active_sessions += 1
        if self.jobs[executor] == "running" and state in ("success", "failed"):
            self.jobs[executor] = state
            self.active_sessions -= 1
            self.processed_sessions += 1
            self.failed_sessions += state == "failed"

    def snapshot(self, draining=False):
        connected = bool(self.websocket is not None and not self.websocket.closed)
        state = self.connection_state
        if state == "connected" and not connected:
            state = "reconnecting"
        return {
            **BUILD_INFO, "mode": "worker", "startedAt": self.started_at,
            "uptimeSeconds": int(time.monotonic() - self.started), "draining": draining,
            "connection": {
                "state": state, "connected": connected, "connections": self.connections,
            },
            "sessions": {
                "started": self.started_sessions, "active": self.active_sessions,
                "processed": self.processed_sessions,
                "completed": self.processed_sessions - self.failed_sessions,
                "failed": self.failed_sessions,
                "scope": "since_process_start",
            },
        }


def status_app(status, draining, sdk_health):
    app = web.Application()

    async def health(_request):
        return web.json_response({"status": "ok", **status.snapshot(draining())},
                                 headers={"Cache-Control": "no-store"})

    async def ready(_request):
        sdk_healthy = await sdk_health()
        snapshot = status.snapshot(draining())
        is_ready = (snapshot["connection"]["connected"] and not snapshot["draining"]
                    and status.connection_state == "connected" and sdk_healthy)
        return web.json_response({
            "status": "ready" if is_ready else "not_ready", "ready": is_ready,
            "sdkHealthy": sdk_healthy, **snapshot,
        }, status=200 if is_ready else 503, headers={"Cache-Control": "no-store"})

    app.add_routes([web.get("/healthz", health), web.get("/status", health),
                    web.get("/readyz", ready)])
    return app
