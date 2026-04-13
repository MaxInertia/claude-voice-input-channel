"""Advisory-lock singleton enforcement for voice-sttd.

Voice-stt refuses to run a second instance. Two instances means two copies
of Whisper loaded into VRAM (~3 GB wasted), two PortAudio mic opens
competing for the same device, and two PTT listener threads both firing
on every keypress — none of which makes sense for the one-user-one-mic
workflow voice-stt is built for.

Enforcement uses ``fcntl.flock`` on a lockfile at
``$XDG_RUNTIME_DIR/voice-stt/daemon.lock``:

* **Advisory, not mandatory.** No kernel-level enforcement, but since we
  control every process that opens this file, advisory is enough.
* **Automatically released on process death.** The kernel tracks the
  holder. ``kill -9`` releases the lock. No stale lockfiles ever,
  regardless of how ungracefully the previous process died — this is the
  failure mode that breaks pidfile-based approaches.
* **Non-blocking.** ``LOCK_NB`` means "fail immediately if held" — no
  waiting, no deadlock potential.

Exit semantics
--------------
``acquire_or_exit`` exits the process with code 2 on contention. The
channel server (``voice-stt-channel.ts``) watches for this specific exit
code and treats it as a "permanent lock contention" sentinel, bypassing
its restart-on-crash retry loop since retrying would just fail the same
way until the first instance exits.
"""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path


def _lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    d = Path(runtime) / "voice-stt"
    d.mkdir(mode=0o700, exist_ok=True)
    # If the directory already existed with looser perms, tighten them.
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d / "daemon.lock"


def acquire_or_exit() -> None:
    """Acquire the singleton lock or exit the process with code 2.

    The returned file descriptor is intentionally leaked — holding it
    open keeps the lock held for the lifetime of the process. When this
    process dies (by any means, including SIGKILL), the kernel releases
    the lock automatically and the next ``acquire_or_exit`` call in a
    fresh process succeeds.
    """
    path = _lock_path()
    # O_CREAT | O_RDWR (not O_TRUNC) — we write the pid into the file for
    # diagnostics but don't rely on it for liveness. The lock itself is
    # authoritative.
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            existing = path.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            existing = "unknown"
        sys.stderr.write(
            f"[voice-sttd] another voice-stt daemon is already running "
            f"(lock held on {path}, previous pid: {existing}).\n"
            f"[voice-sttd] exit the other Claude Code session first, "
            f"or run `kill {existing}` if it's stale.\n"
        )
        os.close(fd)
        sys.exit(2)

    # Record our pid in the lockfile (not load-bearing — just diagnostic).
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    # Intentionally do NOT close fd — holding it open is what keeps the
    # kernel-tracked lock in place. The fd is released when the process
    # exits, which the kernel uses as the signal to drop the lock.
