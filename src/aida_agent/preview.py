"""Configuration status without worker registration, model loading, or provider calls."""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping

VOICE_SETTINGS = (
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "AIDA_STT_MODEL", "AIDA_LLM_MODEL", "AIDA_TTS_MODEL", "AIDA_TTS_VOICE",
)


def status(env: Mapping[str, str]) -> dict:
    missing = [key for key in VOICE_SETTINGS if not env.get(key, "").strip()]
    return {
        "mode": "preview", "voiceEnabled": False,
        "status": "waiting_for_voice_configuration" if missing else "preview_mode",
        "missingSettings": missing,
    }


def make_server(host: str, port: int, env: Mapping[str, str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/healthz", "/readyz"):
                self.send_error(404)
                return
            body = json.dumps(status(env)).encode()
            self.send_response(503 if path == "/readyz" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            # Do not log request paths or attacker-supplied values.
            return

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve status only; never register a voice worker")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    with make_server(args.host, args.port, os.environ) as server:
        print("Aida preview status service started; voice worker registration disabled", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
