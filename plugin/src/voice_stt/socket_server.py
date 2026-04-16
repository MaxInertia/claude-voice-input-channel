"""Unix domain socket server for external IPC with the daemon.

Allows external processes (keyboard shortcuts, CLI tools) to send
``start``, ``stop``, and ``toggle`` commands to the running daemon
without going through the channel server's stdin pipe.

The socket lives at ``<runtime_dir>/voice-stt/daemon.sock``.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

from . import runtime_dir


def _log(msg: str) -> None:
    print(f"[voice-sttd:sock] {msg}", flush=True, file=sys.stderr)


def _sock_path() -> Path:
    return runtime_dir() / "daemon.sock"


class SocketServer:
    def __init__(self, dispatch):
        """Create a socket server.

        ``dispatch`` is called with a single string argument (the command)
        for each connection.  It runs in the listener thread.
        """
        self._dispatch = dispatch
        self._path = _sock_path()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._cleanup_stale()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self._path))
        self._sock.listen(4)
        _log(f"listening on {self._path}")
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="voice-stt-sock",
            daemon=True,
        )
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break  # socket closed during shutdown
            try:
                data = conn.recv(256)
                cmd = data.decode("utf-8", errors="replace").strip()
                if cmd:
                    self._dispatch(cmd)
            except Exception as e:
                _log(f"connection error: {e}")
            finally:
                conn.close()

    def shutdown(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    def _cleanup_stale(self) -> None:
        """Remove a leftover socket file from a previous crashed daemon."""
        if not self._path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self._path))
            # Connection succeeded — another daemon is actually running.
            probe.close()
            _log("stale socket check: another daemon owns the socket")
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            # Nobody home — safe to remove.
            try:
                self._path.unlink()
                _log("removed stale socket file")
            except OSError:
                pass
