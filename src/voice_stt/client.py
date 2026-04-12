"""Voice STT control + consumer CLI.

Subcommands:
  start | stop | toggle | status | quit   — send command to daemon
  listen                                    — print transcribed lines to stdout
  type                                      — type transcribed lines into focused window via xdotool
  clip                                      — copy each line to X clipboard via xclip
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys

from . import CTRL_SOCK, OUT_SOCK


def _send_ctrl(cmd: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(CTRL_SOCK)
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit(f"daemon not running (no socket at {CTRL_SOCK})")
    s.sendall(cmd.encode())
    reply = s.recv(64).decode().strip()
    s.close()
    return reply


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


def cmd_listen():
    for text in _iter_lines(_connect_out()):
        print(text, flush=True)


def cmd_type():
    for text in _iter_lines(_connect_out()):
        # space prefix avoids merging into a previous word; tweak to taste
        subprocess.run(["xdotool", "type", "--delay", "1", "--", text + " "], check=False)


def cmd_clip():
    for text in _iter_lines(_connect_out()):
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=False)
        print(f"[clip] {text}", flush=True)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    for c in ("start", "stop", "toggle", "status", "quit"):
        sub.add_parser(c)
    sub.add_parser("listen", help="print utterances to stdout")
    sub.add_parser("type", help="type utterances into focused window (xdotool)")
    sub.add_parser("clip", help="copy each utterance to clipboard (xclip)")
    args = p.parse_args()

    if args.cmd == "listen":
        cmd_listen()
    elif args.cmd == "type":
        cmd_type()
    elif args.cmd == "clip":
        cmd_clip()
    else:
        print(_send_ctrl(args.cmd))


if __name__ == "__main__":
    main()
