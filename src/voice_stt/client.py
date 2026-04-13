"""Voice STT control + consumer CLI.

Subcommands:
  start | stop   — send command to the daemon over the ctrl socket
  listen         — print transcribed lines to stdout
"""

from __future__ import annotations

import argparse
import socket
import sys

from . import CTRL_SOCK, OUT_SOCK


def _send_ctrl(cmd: str) -> str:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(CTRL_SOCK)
            s.sendall(cmd.encode())
            return s.recv(64).decode().strip()
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit(f"daemon not running (no socket at {CTRL_SOCK})")


def _connect_out() -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(OUT_SOCK)
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit(f"daemon not running (no socket at {OUT_SOCK})")
    return s


def _iter_lines(sock: socket.socket):
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace")
            if text:
                yield text


def cmd_listen() -> None:
    for text in _iter_lines(_connect_out()):
        print(text, flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="tell the daemon to start recording")
    sub.add_parser("stop", help="tell the daemon to stop recording and transcribe")
    sub.add_parser("listen", help="print utterances to stdout")
    args = p.parse_args()

    if args.cmd == "listen":
        cmd_listen()
    else:
        print(_send_ctrl(args.cmd))


if __name__ == "__main__":
    main()
