"""Voice STT daemon.

Holds a faster-whisper model in memory, captures mic audio via PortAudio,
and writes transcribed utterances to stdout, one per line.

This is intended to be spawned as a subprocess by the Claude Code channel
plugin (`plugin/channel/voice-stt-channel.ts`). The channel plugin writes
command lines to this process's stdin (`start\\n` / `stop\\n`), reads
transcripts from its stdout, and forwards stderr to Claude Code's debug
log. The daemon exits cleanly on stdin EOF.

Protocol
--------
stdin  : one command per line. Commands: ``start``, ``stop``. On EOF the
         daemon cleans up and exits with code 0.
stdout : one UTF-8 transcript per line. Each line is a complete utterance
         produced by a single ``start``/``stop`` cycle. No framing header.
stderr : everything else (log lines, errors, model load messages).
exit   : 0 on clean shutdown (stdin EOF), 2 on singleton lock contention
         (another daemon is already running), nonzero on startup failure.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import select
import sys
import threading
import time
from pathlib import Path


def _preload_cuda_libs():
    """Locate cuBLAS/cuDNN shipped via the nvidia-* pip packages and dlopen them
    so ctranslate2 can find them without needing system libcudnn or LD_LIBRARY_PATH."""
    import importlib.util

    for pkg in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
        spec = importlib.util.find_spec(pkg)
        if spec is None or not spec.submodule_search_locations:
            continue
        lib_dir = Path(next(iter(spec.submodule_search_locations)))
        for so in sorted(lib_dir.glob("lib*.so*")):
            try:
                ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


_preload_cuda_libs()

import evdev
import numpy as np
import sounddevice as sd

from . import SAMPLE_RATE

# How often the PTT worker rescans for newly-attached or freshly-recreated
# evdev devices. Keeps the listener self-healing across input-remapper
# reloads (which destroy and recreate the forwarded virtual device) and USB
# replugs. Inherited from the ptt_listener standalone's rescan loop (see
# commit 5a3f391 on the review-fixes branch).
PTT_RESCAN_INTERVAL_SEC = 1.0


def _find_keyboards(trigger: int) -> list[evdev.InputDevice]:
    devs: list[evdev.InputDevice] = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except (PermissionError, OSError):
            continue
        caps = d.capabilities().get(evdev.ecodes.EV_KEY, [])
        if trigger in caps:
            devs.append(d)
    return devs


def _close_device(fd_to_dev: dict[int, evdev.InputDevice], fd: int) -> None:
    dev = fd_to_dev.pop(fd, None)
    if dev is not None:
        try:
            dev.close()
        except Exception:
            pass


def _log(msg: str) -> None:
    """Log to stderr. Stdout is reserved for transcripts."""
    print(f"[voice-sttd] {msg}", flush=True, file=sys.stderr)


class Daemon:
    def __init__(
        self,
        model_name: str,
        device: str,
        compute_type: str,
        input_device: str | int | None,
        ptt_key: str | None = None,
    ):
        from faster_whisper import WhisperModel  # lazy import keeps --help fast

        _log(f"loading model={model_name} device={device} compute={compute_type}")
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _log("model ready")

        # PTT key is optional: None means "no in-process PTT thread" (caller
        # drives start/stop externally via stdin commands). A valid evdev
        # key name spins up the worker thread below.
        self._ptt_key = ptt_key
        self._ptt_trigger: int | None = None
        if ptt_key:
            self._ptt_trigger = evdev.ecodes.ecodes.get(ptt_key)
            if self._ptt_trigger is None:
                _log(f"unknown PTT key {ptt_key!r}; PTT thread disabled")

        self._lock = threading.Lock()
        self._capturing = False
        self._frames: list[np.ndarray] = []

        # Persistent input stream — kept open for the daemon lifetime and
        # gated by self._capturing. See the comments in the Phase 2 refactor
        # of the review-fixes branch for the bugs this prevents (open/close
        # races and virtual-source mute toggling).
        _log(
            f"opening input device: {input_device!r}"
            f" (PULSE_SOURCE={os.environ.get('PULSE_SOURCE', '<unset>')!r})"
        )
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._audio_cb,
            device=input_device,
        )
        self._stream.start()
        _log("audio input stream open (persistent)")

        # Background transcription worker. stop_recording enqueues captured
        # audio and returns immediately so the stdin reader never blocks on
        # Whisper decode.
        self._transcribe_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=8)
        self._transcribe_thread = threading.Thread(
            target=self._transcribe_worker,
            name="voice-stt-transcribe",
            daemon=True,
        )
        self._transcribe_thread.start()

        # PTT evdev listener thread. Watches for the configured key's
        # press/release edges across every keyboard device that advertises
        # the key in its capability list, and calls start_recording /
        # stop_recording directly (no IPC — same process). The thread
        # self-heals across device churn (input-remapper reloads, USB
        # replugs) via a periodic rescan loop.
        if self._ptt_trigger is not None:
            self._ptt_thread = threading.Thread(
                target=self._ptt_worker,
                name="voice-stt-ptt",
                daemon=True,
            )
            self._ptt_thread.start()
        else:
            self._ptt_thread = None

    # ---- audio capture ----

    def _audio_cb(self, indata, frames, time, status):
        if status:
            print(f"[voice-sttd] audio status: {status}", file=sys.stderr, flush=True)
        if self._capturing:
            self._frames.append(indata[:, 0].copy())

    def start_recording(self) -> None:
        with self._lock:
            if self._capturing:
                return
            self._frames = []
            self._capturing = True
        _log("recording: ON")

    def stop_recording(self) -> None:
        with self._lock:
            if not self._capturing:
                return
            self._capturing = False
            audio = np.concatenate(self._frames) if self._frames else np.zeros(0, dtype=np.float32)
            self._frames = []
        _log(f"recording: OFF ({len(audio)/SAMPLE_RATE:.2f}s) — queued")
        try:
            self._transcribe_queue.put_nowait(audio)
        except queue.Full:
            # Pathologically behind worker — drop oldest, prefer recent speech.
            try:
                self._transcribe_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._transcribe_queue.put_nowait(audio)
            except queue.Full:
                _log("transcription queue full, dropping audio")

    def _transcribe_worker(self) -> None:
        while True:
            audio = self._transcribe_queue.get()
            try:
                self._transcribe_and_emit(audio)
            except Exception as e:
                _log(f"transcription error: {e}")
            finally:
                self._transcribe_queue.task_done()

    # ---- PTT evdev listener (in-process thread) ----

    def _ptt_worker(self) -> None:
        """Watch evdev for the configured PTT key's press/release edges.

        Press → self.start_recording(). Release → self.stop_recording().
        Calls are direct method invocations — no IPC since the worker lives
        inside the daemon process. The loop rescans all keyboard devices
        every PTT_RESCAN_INTERVAL_SEC (and immediately after any device
        disappears or is freshly attached), so it self-heals across
        input-remapper reloads and USB replugs.

        Any evdev permission errors at startup are logged but don't crash
        the daemon — the thread just keeps rescanning, and a later udev/
        input-group fix will light it up automatically.
        """
        trigger = self._ptt_trigger
        assert trigger is not None  # guarded in __init__
        key_name = self._ptt_key or f"code={trigger}"

        devs = _find_keyboards(trigger)
        if devs:
            _log(f"PTT: listening for {key_name} on {len(devs)} devices")
            for d in devs:
                _log(f"PTT:   - {d.path}  {d.name}")
        else:
            _log(
                f"PTT: no devices advertise {key_name} yet; will rescan every "
                f"{PTT_RESCAN_INTERVAL_SEC:.1f}s"
            )

        fd_to_dev: dict[int, evdev.InputDevice] = {d.fd: d for d in devs}
        last_scan = time.monotonic()

        while True:
            # Periodic rescan + immediate rescan when there are no devices.
            now = time.monotonic()
            if not fd_to_dev or now - last_scan >= PTT_RESCAN_INTERVAL_SEC:
                new_devs = _find_keyboards(trigger)
                new_paths = {d.path for d in new_devs}
                old_paths = {d.path for d in fd_to_dev.values()}
                added = [d for d in new_devs if d.path not in old_paths]
                for d in added:
                    fd_to_dev[d.fd] = d
                for fd in list(fd_to_dev):
                    if fd_to_dev[fd].path not in new_paths:
                        _close_device(fd_to_dev, fd)
                if added:
                    _log(f"PTT: attached {len(added)} new device(s)")
                    for d in added:
                        _log(f"PTT:   + {d.path}  {d.name}")
                last_scan = now

            if not fd_to_dev:
                time.sleep(PTT_RESCAN_INTERVAL_SEC)
                continue

            try:
                r, _, _ = select.select(list(fd_to_dev), [], [], PTT_RESCAN_INTERVAL_SEC)
            except (OSError, ValueError):
                # A watched fd went invalid between iterations. Force a
                # full rescan on the next loop pass.
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
                    _log(f"PTT: device {dev.path} gone ({e}); dropping")
                    _close_device(fd_to_dev, fd)
                    last_scan = 0.0  # force rescan next pass
                    continue

                for ev in events:
                    if ev.type != evdev.ecodes.EV_KEY or ev.code != trigger:
                        continue
                    if ev.value == 1:  # key down
                        _log("PTT: press → start")
                        self.start_recording()
                    elif ev.value == 0:  # key up
                        _log("PTT: release → stop")
                        self.stop_recording()
                    # ev.value == 2 is autorepeat, ignore

    def _transcribe_and_emit(self, audio: np.ndarray) -> None:
        if audio.size < SAMPLE_RATE // 4:  # <0.25s, skip
            _log("audio too short, skipping")
            return
        segments, info = self.model.transcribe(
            audio,
            language="en",
            vad_filter=True,
            beam_size=5,
        )
        # Join all segments from one recording into a single output line.
        # One PTT press = one thought = one transcript = one channel event.
        text = "".join(seg.text for seg in segments).strip()
        if not text:
            return
        _log(f"> {text}")
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            # Parent closed the pipe — nothing to forward to, exit cleanly
            # on the next stdin read (which will also see EOF).
            pass

    # ---- lifecycle ----

    def run_stdin_loop(self) -> None:
        """Main loop: read line-delimited commands from stdin until EOF.

        Commands:
            ``start``  — begin recording
            ``stop``   — stop recording and queue for transcription
            (unknown commands are silently ignored; the daemon never
             trusts its own stdin for anything beyond these two verbs)
        """
        _log("ready; awaiting stdin commands")
        for raw in sys.stdin:
            cmd = raw.strip()
            if cmd == "start":
                self.start_recording()
            elif cmd == "stop":
                self.stop_recording()
            elif cmd == "":
                continue
            else:
                _log(f"unknown command: {cmd!r}")

    def shutdown(self) -> None:
        """Best-effort cleanup — called from main()'s finally block."""
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


def _parse_input_device(raw: str | None) -> str | int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def _bridge_pulse_source() -> None:
    """Translate VOICE_STT_PULSE_SOURCE into PULSE_SOURCE for PortAudio.

    PortAudio's pulseaudio host API reads PULSE_SOURCE at stream-open time
    to pick a specific capture source. We expose this to users as
    VOICE_STT_PULSE_SOURCE (namespaced under the project) and bridge it
    here so the daemon can be configured via the XDG config file alone.
    """
    if "PULSE_SOURCE" in os.environ:
        return
    ps = os.environ.get("VOICE_STT_PULSE_SOURCE", "").strip()
    if ps:
        os.environ["PULSE_SOURCE"] = ps


def main():
    # Config precedence (highest to lowest):
    #     CLI arg > shell env > XDG config file > builtin default
    _bridge_pulse_source()

    p = argparse.ArgumentParser()
    p.add_argument(
        "--model",
        default=os.environ.get("VOICE_STT_MODEL", "medium.en"),
        help="faster-whisper model name (e.g. small.en, medium.en, large-v3). "
             "Env: VOICE_STT_MODEL.",
    )
    p.add_argument(
        "--device",
        default=os.environ.get("VOICE_STT_COMPUTE_DEVICE", "cuda"),
        help="CTranslate2 inference device: cuda or cpu. "
             "Env: VOICE_STT_COMPUTE_DEVICE.",
    )
    p.add_argument(
        "--compute-type",
        default=os.environ.get("VOICE_STT_COMPUTE_TYPE", "float16"),
        help="CTranslate2 compute type: float16 (GPU), int8_float16, int8 (CPU). "
             "Env: VOICE_STT_COMPUTE_TYPE.",
    )
    p.add_argument(
        "--input-device",
        default=os.environ.get("VOICE_STT_INPUT_DEVICE", "pulse"),
        help=(
            "Audio input device passed to sounddevice.InputStream(device=...). "
            "Accepts a numeric index or a substring of the device name. "
            "Default 'pulse' routes through the pulseaudio compat layer and "
            "honors PULSE_SOURCE. Pass 'default' to use the system default "
            "without the PULSE_SOURCE indirection. "
            "Env: VOICE_STT_INPUT_DEVICE."
        ),
    )
    p.add_argument(
        "--ptt-key",
        default=os.environ.get("VOICE_STT_PTT_KEY", "KEY_F20"),
        help=(
            "evdev key name the in-process PTT thread watches for press/"
            "release. F13-F24 are unused on most keyboards and make good "
            "intermediary keys for remapper tools (input-remapper, xremap, "
            "kmonad) to target. Pass an empty string to disable the PTT "
            "thread entirely (useful if the parent process drives start/"
            "stop via stdin). "
            "Env: VOICE_STT_PTT_KEY."
        ),
    )
    args = p.parse_args()

    input_device = _parse_input_device(
        args.input_device if args.input_device != "default" else None
    )
    ptt_key = args.ptt_key or None  # empty string -> None -> no PTT thread
    d = Daemon(args.model, args.device, args.compute_type, input_device, ptt_key)
    try:
        d.run_stdin_loop()
    except KeyboardInterrupt:
        _log("interrupted")
    finally:
        d.shutdown()
        _log("bye")


if __name__ == "__main__":
    main()
