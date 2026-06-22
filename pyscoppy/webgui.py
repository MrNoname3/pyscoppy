"""Web GUI bridge: serves an oscilloscope UI and relays to/from scoppyd.

A browser-based scope styled after the Scoppy Android app. It connects to the
same daemon as the CLI/agent, so the human and the agent share one live stream
and stay in sync. Stdlib only (http.server + Server-Sent Events).

Run:  python3 -m pyscoppy gui     then open http://127.0.0.1:8077
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .dclient import DaemonClient, is_daemon_running

WEBDIR = os.path.join(os.path.dirname(__file__), "web")
_CT = {".html": "text/html", ".js": "application/javascript", ".css": "text/css"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:   # silence the access log
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # always serve fresh UI during dev
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/index.html"
        if path == "/events":
            return self._sse()
        if path == "/state":
            return self._state()
        fn = os.path.join(WEBDIR, os.path.basename(path))
        if os.path.isfile(fn):
            ext = os.path.splitext(fn)[1]
            with open(fn, "rb") as f:
                return self._send(200, _CT.get(ext, "application/octet-stream"), f.read())
        self._send(404, "text/plain", b"not found")

    def do_POST(self) -> None:
        if self.path != "/cmd":
            return self._send(404, "text/plain", b"not found")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            cmd: dict[str, Any] = json.loads(body)
        except Exception:
            return self._send(400, "text/plain", b"bad json")
        if not is_daemon_running():
            return self._send(503, "application/json", b'{"error":"daemon not running"}')
        try:
            c = DaemonClient(role="web")
            if cmd.get("cmd") == "grab":
                m = c.grab(channel=cmd.get("channel", 0), n=cmd.get("n", 2000))
                c.close()
                return self._send(200, "application/json", json.dumps(m or {}).encode())
            c.send(cmd)
            c.close()
            return self._send(200, "application/json", b'{"ok":true}')
        except Exception as e:
            return self._send(500, "application/json", json.dumps({"error": str(e)}).encode())

    def _state(self) -> None:
        if not is_daemon_running():
            return self._send(503, "application/json", b'{"error":"daemon not running"}')
        c = DaemonClient(role="web")
        st = c.get_state()
        c.close()
        self._send(200, "application/json", json.dumps({"state": st}).encode())

    def _sse(self) -> None:
        if not is_daemon_running():
            return self._send(503, "text/plain", b"daemon not running")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        client = DaemonClient(role="web")
        client.subscribe()
        try:
            # idle_ok keeps the stream open across quiet periods (a device resync can
            # block the daemon for a few seconds); on idle we send an SSE keepalive
            # comment, which also surfaces a closed browser tab as a write error.
            for msg in client.messages(timeout=1.0, idle_ok=True):
                if msg is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(("data: " + json.dumps(msg) + "\n\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            client.close()


def make_server(host: str = "127.0.0.1", port: int = 8077) -> ThreadingHTTPServer:
    """Build the GUI HTTP server (caller drives serve_forever / shutdown)."""
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def run(host: str = "127.0.0.1", port: int = 8077) -> None:
    if not is_daemon_running():
        print("WARNING: scoppyd not running. Start it:  python3 -m pyscoppy daemon")
    try:
        srv = make_server(host, port)
    except OSError as e:
        # port already bound — almost always the GUI is already serving here
        print(f"Can't bind {host}:{port} ({e}). Is the GUI already up? "
              f"Open http://{host}:{port}")
        return
    print(f"scoppy web GUI: http://{host}:{port}  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
