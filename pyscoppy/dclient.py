"""Client for talking to scoppyd over its Unix socket (used by CLI and TUI)."""

import json
import socket
from typing import Iterator, Literal, Optional, overload

from .daemon import DEFAULT_SOCK


class DaemonClient:
    def __init__(self, sock_path=DEFAULT_SOCK, role="agent"):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(sock_path)
        self.sock.setblocking(True)
        self._buf = b""
        self.send({"cmd": "hello", "role": role})

    def fileno(self):
        return self.sock.fileno()

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    @overload
    def messages(self, timeout: Optional[float] = ...,
                 idle_ok: Literal[False] = ...) -> Iterator[dict]: ...
    @overload
    def messages(self, timeout: Optional[float] = ..., *,
                 idle_ok: Literal[True]) -> Iterator[Optional[dict]]: ...

    def messages(self, timeout=None, idle_ok=False) -> Iterator[Optional[dict]]:
        """Yield decoded messages as they arrive. Blocks; set socket timeout
        via `timeout` (seconds) to make it return periodically.

        With idle_ok=True the generator survives quiet periods (e.g. a multi-second
        device resync): on a read timeout it yields None instead of returning, so a
        long-lived consumer (the SSE bridge) stays connected and only stops on a real
        EOF. Without it (the default), a timeout ends the generator.
        """
        self.sock.settimeout(timeout)
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if line.strip():
                    yield json.loads(line)
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                if idle_ok:
                    yield None
                    continue
                return
            if not data:
                return
            self._buf += data

    def request(self, obj, want_type, timeout=3.0):
        """Send a command and return the first reply of `want_type`."""
        self.send(obj)
        for msg in self.messages(timeout=timeout):
            if msg.get("type") == want_type:
                return msg
        return None

    # convenience
    def get_state(self, timeout=0.4):
        """Return the latest state (drains any stale buffered state messages)."""
        self.send({"cmd": "get_state"})
        latest = None
        for msg in self.messages(timeout=timeout):
            if msg.get("type") == "state":
                latest = msg["state"]
        return latest

    def set(self, **params):
        self.send({"cmd": "set", "params": params})

    def grab(self, channel=0, n=2000, timeout=5.0):
        return self.request({"cmd": "grab", "channel": channel, "n": n}, "grab", timeout)

    def subscribe(self):
        self.send({"cmd": "subscribe"})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def is_daemon_running(sock_path=DEFAULT_SOCK):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
        s.close()
        return True
    except OSError:
        return False
