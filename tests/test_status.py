import asyncio
import gc
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from livekit.agents import AgentServer, utils
from livekit.agents.ipc.job_executor import JobStatus
from livekit.protocol import agent

from aida_agent.monitored_server import MonitoredAgentServer
from aida_agent.status import WorkerStatus, status_app


class Executor:
    def __init__(self, job_id="job-1", status=JobStatus.RUNNING):
        self.running_job = SimpleNamespace(job=SimpleNamespace(id=job_id))
        self.status = status


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


async def test_health_and_readiness_report_connection_sessions_and_sdk_health():
    state = WorkerStatus()
    draining = False
    sdk_health = AsyncMock(return_value=True)
    runner, port = await serve(status_app(state, lambda: draining, sdk_health))
    try:
        async with aiohttp.ClientSession() as client:
            async def get(path):
                async with client.get(f"http://127.0.0.1:{port}{path}") as res:
                    assert res.headers["Cache-Control"] == "no-store"
                    return res.status, await res.json()

            code, body = await get("/healthz")
            assert code == 200
            assert body["connection"] == {
                "connected": False, "state": "starting", "connections": 0,
            }
            assert body["sessions"]["active"] == body["sessions"]["processed"] == 0
            assert "version" in body and body["timeZone"] == "America/Los_Angeles"
            sdk_health.assert_not_awaited()
            assert (await get("/readyz"))[0] == 503
            socket = SimpleNamespace(closed=False)
            state.connected(socket)
            first, second = Executor(), Executor("job-2")
            state.observe_job(first, "running")
            state.observe_job(second, "running")
            state.observe_job(first, "success")
            code, body = await get("/readyz")
            assert code == 200 and body["ready"] is True
            assert body["sessions"] == {"started": 2, "active": 1, "processed": 1,
                                         "completed": 1, "failed": 0,
                                         "scope": "since_process_start"}
            assert "job-1" not in str(body) and "job-2" not in str(body)
            sdk_health.return_value = False
            assert (await get("/readyz"))[0] == 503
            sdk_health.return_value = True
            draining = True
            assert (await get("/readyz"))[0] == 503
            draining = False
            socket.closed = True
            # Closure is visible even before the SDK's receive loop finishes unwinding.
            assert (await get("/readyz"))[0] == 503
            assert (await get("/status"))[1]["connection"]["connected"] is False
            assert (await get("/healthz"))[0] == 200
            async with client.get(f"http://127.0.0.1:{port}/unknown") as res:
                assert res.status == 404
    finally:
        await runner.cleanup()


async def test_parent_job_callbacks_count_finished_failed_and_duplicate_notifications(monkeypatch):
    server = MonitoredAgentServer()
    monkeypatch.setattr(server, "_queue_msg", AsyncMock())
    first, second = Executor(), Executor("job-2")
    await server._update_job_status(first)
    await server._update_job_status(first)
    await server._update_job_status(second)
    first.status = JobStatus.SUCCESS
    await server._update_job_status(first)
    await server._update_job_status(first)
    second.status = JobStatus.FAILED
    await server._update_job_status(second)
    body = server.status.snapshot()
    assert body["sessions"] == {"started": 2, "active": 0, "processed": 2,
                                "completed": 1, "failed": 1, "scope": "since_process_start"}
    assert server._queue_msg.await_count == 6
    # A very short/crashed job may be observed only when its executor closes.
    await server._update_job_status(Executor("job-3", JobStatus.FAILED))
    assert server.status.snapshot()["sessions"]["processed"] == 3
    # Instrumentation does not retain historical executor objects or job metadata.
    del first, second
    gc.collect()
    assert len(server.status.jobs) == 0
    assert WorkerStatus().snapshot()["sessions"]["started"] == 0


async def test_real_sdk_websocket_registration_disconnect_and_reconnect(monkeypatch):
    first_registered = asyncio.Event()
    close_first = asyncio.Event()
    second_connected = asyncio.Event()
    register_second = asyncio.Event()
    hold_second = asyncio.Event()
    connections = 0

    async def handle(request):
        nonlocal connections
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        connections += 1
        request_message = agent.WorkerMessage()
        request_message.ParseFromString(await socket.receive_bytes())
        assert request_message.HasField("register")
        if connections == 2:
            second_connected.set()
            await register_second.wait()
        reply = agent.ServerMessage()
        reply.register.worker_id = f"worker-{connections}"
        await socket.send_bytes(reply.SerializeToString())
        if connections == 1:
            first_registered.set()
            await close_first.wait()
        else:
            await hold_second.wait()
        await socket.close()
        return socket

    app = web.Application()
    app.router.add_get("/agent", handle)
    runner, port = await serve(app)
    server = MonitoredAgentServer(ws_url=f"ws://127.0.0.1:{port}", api_key="test",
                                  api_secret="test-secret-" * 4, http_proxy=None)
    server._closed = False
    server._loop = asyncio.get_running_loop()
    server._msg_chan = utils.aio.Chan()
    monkeypatch.setattr(server, "_report_active_jobs", AsyncMock())
    monkeypatch.setattr(server, "_update_worker_status", AsyncMock())
    try:
        async with aiohttp.ClientSession() as client:
            server._http_session = client
            task = asyncio.create_task(server._connection_task())
            try:
                async with asyncio.timeout(5):
                    await first_registered.wait()
                    while not server.status.snapshot()["connection"]["connected"]:
                        await asyncio.sleep(0.01)
                    close_first.set()
                    await second_connected.wait()
                    assert server.status.snapshot()["connection"] == {
                        "connected": False, "state": "reconnecting", "connections": 1,
                    }
                    register_second.set()
                    while server.status.connections != 2:
                        await asyncio.sleep(0.01)
                    assert server.status.snapshot()["connection"]["connected"] is True
                    assert server.status.snapshot()["sessions"]["started"] == 0
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                assert server.status.snapshot()["connection"]["connected"] is False
    finally:
        close_first.set()
        register_second.set()
        hold_second.set()
        await runner.cleanup()


async def test_listener_lifecycle_closes_on_sdk_start_failure(monkeypatch):
    server = MonitoredAgentServer(status_host="127.0.0.1", status_port=0)

    async def fail_run(_self, **_kwargs):
        port = server.status_runner.addresses[0][1]
        async with aiohttp.ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/healthz") as res:
                assert res.status == 200
            async with client.get(f"http://127.0.0.1:{port}/readyz") as res:
                assert res.status == 503
        raise RuntimeError("startup failed")

    monkeypatch.setattr(AgentServer, "run", fail_run)
    with pytest.raises(RuntimeError, match="startup failed"):
        await server.run()
    assert server.status_runner.addresses == []
    assert server.status_client.closed
    assert server.status.snapshot()["connection"]["state"] == "stopped"


async def test_sdk_readiness_probe_uses_actual_local_health_status():
    response_code = 503
    app = web.Application()
    async def health(_request):
        return web.Response(status=response_code)

    app.router.add_get("/", health)
    runner, port = await serve(app)
    server = MonitoredAgentServer()
    server._http_server = SimpleNamespace(port=port)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.5)) as client:
            server.status_client = client
            assert await server.sdk_health() is False
            response_code = 200
            assert await server.sdk_health() is True
        assert await server.sdk_health() is False
    finally:
        await runner.cleanup()


def test_status_port_validation_and_sdk_upgrade_guard(monkeypatch):
    from aida_agent import monitored_server, worker

    for value in ["invalid", "0", "8081", "65536"]:
        monkeypatch.setenv("AIDA_STATUS_PORT", value)
        with pytest.raises(ValueError):
            worker.make_server()
    monkeypatch.setattr(monitored_server, "version", lambda _name: "1.9.0")
    with pytest.raises(RuntimeError, match="Review"):
        MonitoredAgentServer()


async def test_terminal_connection_failure_and_shutdown_never_report_connected(monkeypatch):
    server = MonitoredAgentServer()
    monkeypatch.setattr(AgentServer, "_connection_task", AsyncMock(side_effect=RuntimeError("failed")))
    with pytest.raises(RuntimeError):
        await server._connection_task()
    assert server.status.snapshot()["connection"]["state"] == "failed"
    assert server.status.snapshot()["connection"]["connected"] is False
    server.status.connected(SimpleNamespace(closed=False))
    monkeypatch.setattr(AgentServer, "aclose", AsyncMock())
    await server.aclose()
    assert server.status.snapshot()["connection"]["state"] == "stopping"
    assert server.status.snapshot()["connection"]["connected"] is False
