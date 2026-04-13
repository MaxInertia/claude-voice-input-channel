"""Push-to-talk listener.

Watches all keyboard input devices for a configured key (default KEY_F20)
and drives hold-to-talk: press → tells the voice-sttd daemon to start
recording, release → tells it to stop and transcribe.

The expected setup is to pair this with a key remapper (input-remapper,
xremap, kmonad, etc.) that rewrites whatever hardware button you actually
want to press into the target key. KEY_F20 is a sensible default because
F13–F24 are almost never bound to anything and won't collide with typing.

Requires read access to /dev/input/event*. Easiest fix:
    sudo usermod -aG input $USER
    # log out and back in
"""

from __future__ import annotations

import argparse
import os
import select
import socket
import sys
import time

import evdev

from . import CTRL_SOCK

# Rescan interval when there are no devices to watch, or when a read fails
# and we need to find replacement devices. Keeps the listener self-healing
# across input-remapper reloads and USB replugs.
RESCAN_INTERVAL_SEC = 1.0


def _send(cmd: str) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(CTRL_SOCK)
            s.sendall(cmd.encode())
            s.recv(64)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        print(f"[ptt] daemon not reachable ({e}); is voice-sttd running?", file=sys.stderr)


def _find_keyboards(trigger: int) -> list[evdev.InputDevice]:
    devs = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except PermissionError:
            print(f"[ptt] no permission to read {path} (add yourself to the input group)", file=sys.stderr)
            continue
        caps = d.capabilities().get(evdev.ecodes.EV_KEY, [])
        # any device that could possibly emit our trigger
        if trigger in caps:
            devs.append(d)
    return devs


def _close_device(fd_to_dev: dict[int, evdev.InputDevice], fd: int) -> None:
    """Drop a dead fd from the watch map and close the underlying device."""
    dev = fd_to_dev.pop(fd, None)
    if dev is not None:
        try:
            dev.close()
        except Exception:
            pass


def _log_devices(action: str, devs: list[evdev.InputDevice]) -> None:
    print(f"[ptt] {action} ({len(devs)} devices):", flush=True)
    for d in devs:
        print(f"       - {d.path}  {d.name}", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--key",
        default=os.environ.get("VOICE_STT_PTT_KEY", "KEY_F20"),
        help="evdev key name to watch for press/release. "
             "Env: VOICE_STT_PTT_KEY. Default: KEY_F20.",
    )
    args = p.parse_args()

    trigger = evdev.ecodes.ecodes.get(args.key)
    if trigger is None:
        sys.exit(f"unknown key: {args.key}")

    # Initial device scan. If nothing advertises the trigger key yet, don't
    # die — input-remapper may not have loaded its preset. Wait for it.
    devs = _find_keyboards(trigger)
    if devs:
        _log_devices(f"listening for {args.key}", devs)
    else:
        print(
            f"[ptt] no devices advertise {args.key} yet; will rescan every "
            f"{RESCAN_INTERVAL_SEC:.1f}s",
            flush=True,
        )

    fd_to_dev: dict[int, evdev.InputDevice] = {d.fd: d for d in devs}
    last_scan = time.monotonic()

    while True:
        # Rescan periodically (and immediately when we have no devices).
        # Handles:
        #   - Cold start before input-remapper has loaded its preset
        #   - input-remapper reloading and destroying its forwarded virtual
        #     device (old fd goes dead, new fd appears)
        #   - USB keyboards being plugged/unplugged mid-session
        now = time.monotonic()
        if not fd_to_dev or now - last_scan >= RESCAN_INTERVAL_SEC:
            new_devs = _find_keyboards(trigger)
            new_paths = {d.path for d in new_devs}
            old_paths = {d.path for d in fd_to_dev.values()}
            # Add devices that weren't being watched yet.
            added = [d for d in new_devs if d.path not in old_paths]
            for d in added:
                fd_to_dev[d.fd] = d
            # Drop devices that no longer advertise the trigger.
            for fd in list(fd_to_dev):
                if fd_to_dev[fd].path not in new_paths:
                    _close_device(fd_to_dev, fd)
            if added:
                _log_devices("attached", added)
            last_scan = now

        if not fd_to_dev:
            time.sleep(RESCAN_INTERVAL_SEC)
            continue

        try:
            r, _, _ = select.select(list(fd_to_dev), [], [], RESCAN_INTERVAL_SEC)
        except (OSError, ValueError):
            # A watched fd went invalid between iterations. Force a full rescan.
            for fd in list(fd_to_dev):
                _close_device(fd_to_dev, fd)
            last_scan = 0.0
            continue

        for fd in r:
            dev = fd_to_dev.get(fd)
            if dev is None:
                continue
            try:
                events = list(dev.read())
            except OSError as e:
                print(f"[ptt] device {dev.path} gone ({e}); dropping", flush=True)
                _close_device(fd_to_dev, fd)
                last_scan = 0.0  # force rescan next iteration
                continue

            for ev in events:
                if ev.type != evdev.ecodes.EV_KEY or ev.code != trigger:
                    continue
                if ev.value == 1:  # key down
                    print("[ptt] press → start", flush=True)
                    _send("start")
                elif ev.value == 0:  # key up
                    print("[ptt] release → stop", flush=True)
                    _send("stop")
                # ev.value == 2 is autorepeat, ignore


if __name__ == "__main__":
    main()
