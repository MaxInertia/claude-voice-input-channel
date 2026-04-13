"""voice-stt package constants and socket paths.

Sockets live under $XDG_RUNTIME_DIR/voice-stt/ on standard systemd-based
Linux desktops (XDG_RUNTIME_DIR is typically /run/user/$UID, per-user and
0700 by design). Falls back to /tmp/voice-stt/ if XDG_RUNTIME_DIR is unset,
which would be an unusual non-interactive environment.

The daemon creates the parent directory with 0700 perms at startup. Putting
the sockets in a 0700 parent dir closes the bind→chmod race that existed
when sockets lived directly in /tmp — between `bind()` and the subsequent
`chmod 0o600`, the socket inherited umask-derived perms and was briefly
reachable by any local user.
"""

from __future__ import annotations

import os

_runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
SOCKET_DIR = os.path.join(_runtime_dir, "voice-stt")
CTRL_SOCK = os.path.join(SOCKET_DIR, "ctrl.sock")
OUT_SOCK = os.path.join(SOCKET_DIR, "out.sock")

SAMPLE_RATE = 16000
