"""Status adapter for pinned LiveKit 1.8.0; voice behavior remains in AgentServer.

The SDK exposes registration but no public disconnect/job-status events. Keep its
three internal lifecycle hooks here, covered by real SDK/WebSocket tests. Review
this adapter when upgrading livekit-agents; do not infer connectivity from uptime.
"""

from importlib.metadata import version

from aiohttp import ClientSession, ClientTimeout, web
from livekit.agents import AgentServer
from livekit.agents.ipc.job_executor import JobStatus

from .status import WorkerStatus, status_app


class MonitoredAgentServer(AgentServer):
    def __init__(self, *, status_host="0.0.0.0", status_port=8082, **kwargs):
        if version("livekit-agents") != "1.8.0":
            raise RuntimeError("Review the worker status adapter before upgrading livekit-agents")
        super().__init__(**kwargs)
        self.status = WorkerStatus()
        self.status_host = status_host
        self.status_port = status_port
        self.status_runner = None
        self.status_client = None

    async def sdk_health(self):
        port = self.worker_info.http_port
        if not port or self.status_client is None:
            return False
        try:
            async with self.status_client.get(f"http://127.0.0.1:{port}/") as response:
                return response.status == 200
        except (OSError, TimeoutError):
            return False
        except Exception:
            # HTTP failures are readiness failures, never expose SDK/network exception text.
            return False

    async def run(self, *, devmode=False, unregistered=False):
        self.status_client = ClientSession(timeout=ClientTimeout(total=0.5), trust_env=False)
        self.status_runner = web.AppRunner(
            status_app(self.status, lambda: self.draining, self.sdk_health), access_log=None,
        )
        try:
            await self.status_runner.setup()
            site = web.TCPSite(self.status_runner, self.status_host, self.status_port)
            await site.start()
            await super().run(devmode=devmode, unregistered=unregistered)
        finally:
            self.status.connection_state = "stopped"
            self.status.websocket = None
            await self.status_runner.cleanup()
            await self.status_client.close()

    async def aclose(self):
        self.status.connection_state = "stopping"
        self.status.websocket = None
        await super().aclose()

    async def _connection_task(self):
        self.status.connection_state = "connecting"
        try:
            await super()._connection_task()
        except Exception:
            self.status.connection_state = "failed"
            raise
        finally:
            self.status.disconnected()

    async def _run_ws(self, websocket):
        # The SDK calls this only after a successful registration handshake.
        self.status.connected(websocket)
        try:
            await super()._run_ws(websocket)
        finally:
            self.status.disconnected()

    async def _update_job_status(self, executor):
        if executor.running_job is not None:
            state = {JobStatus.RUNNING: "running", JobStatus.SUCCESS: "success",
                     JobStatus.FAILED: "failed"}.get(executor.status)
            if state:
                self.status.observe_job(executor, state)
        await super()._update_job_status(executor)
