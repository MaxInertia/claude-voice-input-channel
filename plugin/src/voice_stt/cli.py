"""Thin CLI to send commands to the running voice-stt daemon via Unix socket.

Usage:
    voice-stt              # defaults to "toggle"
    voice-stt toggle       # toggle recording on/off
    voice-stt start        # start recording
    voice-stt stop         # stop recording
"""

from __future__ import annotations

import socket
import sys

from . import runtime_dir


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    sock_path = runtime_dir() / "daemon.sock"

    if not sock_path.exists():
        print(
            f"voice-stt: daemon socket not found at {sock_path}\n"
            f"Is voice-sttd running?",
            file=sys.stderr,
        )
        sys.exit(1)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(sock_path))
        sock.sendall((cmd + "\n").encode())
    except ConnectionRefusedError:
        print("voice-stt: daemon is not running", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"voice-stt: socket error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
