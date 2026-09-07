import http.client
import json
import os
import subprocess
import sys
import threading

from aida_agent.preview import VOICE_SETTINGS, make_server, status


def test_status_reveals_only_missing_names_and_never_enables_voice():
    secret = "private-test-value"
    env = dict.fromkeys(VOICE_SETTINGS, secret)
    env["AIDA_LLM_MODEL"] = "   "
    result = status(env)
    assert result["missingSettings"] == ["AIDA_LLM_MODEL"]
    assert result["status"] == "waiting_for_voice_configuration"
    assert secret not in json.dumps(result)
    env["AIDA_LLM_MODEL"] = secret
    assert status(env)["voiceEnabled"] is False
    assert status(env)["status"] == "preview_mode"


def test_preview_health_is_live_but_never_ready_for_voice():
    server = make_server("127.0.0.1", 0, {})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path, expected in [("/healthz", 200), ("/readyz", 503), ("/unknown", 404)]:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            assert response.status == expected
            if path != "/unknown":
                assert json.loads(body)["voiceEnabled"] is False
                assert response.getheader("Cache-Control") == "no-store"
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_preview_cli_does_not_import_livekit_or_worker():
    result = subprocess.run(
        [sys.executable, "-c", """
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith('livekit') or name in ('worker', 'aida_agent.worker'):
        raise AssertionError('preview must not import voice worker or SDK')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = ['aida-agent', 'preview', '--help']
from aida_agent.cli import main
main()
"""], check=False, capture_output=True, text=True, env=os.environ,
    )
    assert result.returncode == 0, result.stderr
    assert "never register a voice worker" in result.stdout
