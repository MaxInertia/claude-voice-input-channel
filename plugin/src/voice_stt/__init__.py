"""voice-stt package constants."""

import os
import sys
from pathlib import Path

SAMPLE_RATE = 16000


def runtime_dir() -> Path:
    """Return the platform-appropriate runtime directory for sockets and locks.

    Linux: $XDG_RUNTIME_DIR (typically /run/user/<uid>), falls back to /tmp.
    macOS: $TMPDIR (per-user private dir, e.g. /var/folders/xx/.../T/).
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or (
        os.environ.get("TMPDIR", "/tmp") if sys.platform == "darwin" else "/tmp"
    )
    d = Path(base) / "voice-stt"
    d.mkdir(mode=0o700, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d
