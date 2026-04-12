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
import select
import socket
import sys

import evdev

from . import CTRL_SOCK


def _send(cmd: str) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(CTRL_SOCK)
        s.sendall(cmd.encode())
        s.recv(64)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        print(f"[ptt] daemon not reachable ({e}); is voice-sttd running?", file=sys.stderr)
    finally:
        s.close()


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--key",
        default="KEY_F20",
        help="evdev key name to watch for press/release (default: KEY_F20)",
    )
    args = p.parse_args()

    trigger = evdev.ecodes.ecodes.get(args.key)
    if trigger is None:
        sys.exit(f"unknown key: {args.key}")

    devs = _find_keyboards(trigger)
    if not devs:
        sys.exit(
            f"no input devices advertise {args.key}. "
            "Make sure input-remapper is running and the preset is loaded."
        )
    print(f"[ptt] listening for {args.key} on {len(devs)} devices:", flush=True)
    for d in devs:
        print(f"       - {d.path}  {d.name}", flush=True)

    fd_to_dev = {d.fd: d for d in devs}
    while True:
        r, _, _ = select.select(fd_to_dev, [], [])
        for fd in r:
            for ev in fd_to_dev[fd].read():
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
