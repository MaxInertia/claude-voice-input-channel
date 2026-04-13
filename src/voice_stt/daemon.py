"""Voice STT daemon.

Holds a faster-whisper model in memory. Listens on a control Unix socket for
start/stop commands. While "started", captures mic audio. On stop, transcribes
the buffered audio and broadcasts each line of text to all clients connected
to the output Unix socket.

Output protocol: newline-delimited UTF-8 text. One utterance per line.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import socket
import sys
import threading
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

import numpy as np
import sounddevice as sd

from . import CTRL_SOCK, OUT_SOCK, SAMPLE_RATE, SOCKET_DIR


class Daemon:
    def __init__(
        self,
        model_name: str,
        device: str,
        compute_type: str,
        input_device: str | int | None,
    ):
        from faster_whisper import WhisperModel  # imported lazily so --help is fast

        print(f"[voice-sttd] loading model={model_name} device={device} compute={compute_type}", flush=True)
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        print("[voice-sttd] model ready", flush=True)

        self._lock = threading.Lock()
        self._capturing = False
        self._frames: list[np.ndarray] = []

        # Persistent input stream opened at startup and kept open for the
        # daemon lifetime. The callback gates on self._capturing so we only
        # buffer frames while a PTT press is active, but the stream itself
        # never reopens. This avoids two independent bugs we hit on PipeWire:
        #
        #   1. Open/close races — PortAudio's device release didn't finish
        #      before the next open, so alternating opens delivered a
        #      half-released handle producing all-zero samples.
        #   2. Virtual PipeWire sources (noise cancellation, echo
        #      cancellation, EQ, etc.) can toggle their source mute state
        #      between consumer sessions, producing alternating zero buffers
        #      even with a persistent stream. Setting `PULSE_SOURCE` to a
        #      raw hardware input (see scripts/voice-stt-svc) bypasses the
        #      effect node entirely.
        print(
            f"[voice-sttd] opening input device: {input_device!r}"
            f" (PULSE_SOURCE={os.environ.get('PULSE_SOURCE', '<unset>')!r})",
            flush=True,
        )
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._audio_cb,
            device=input_device,
        )
        self._stream.start()
        print("[voice-sttd] audio input stream open (persistent)", flush=True)

        self._out_clients: list[socket.socket] = []
        self._out_lock = threading.Lock()

        # Background transcription worker. stop_recording enqueues a
        # captured audio array and returns immediately, so ctrl-handler
        # threads never block on Whisper decode. A single worker drains
        # the queue — serializing transcriptions is what we want anyway,
        # because faster-whisper/CTranslate2 isn't safe for concurrent
        # calls on one model instance.
        self._transcribe_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=8)
        self._transcribe_thread = threading.Thread(
            target=self._transcribe_worker,
            name="voice-stt-transcribe",
            daemon=True,
        )
        self._transcribe_thread.start()

    # ---- audio capture ----

    def _audio_cb(self, indata, frames, time, status):
        if status:
            print(f"[voice-sttd] audio status: {status}", file=sys.stderr, flush=True)
        # The stream is always open; only buffer frames while capturing. No
        # lock here — _capturing is a single bool read, and _frames is only
        # touched by this callback and the ctrl threads in start/stop which
        # do so while _capturing is False.
        if self._capturing:
            self._frames.append(indata[:, 0].copy())

    def start_recording(self):
        with self._lock:
            if self._capturing:
                return
            self._frames = []
            self._capturing = True
        print("[voice-sttd] recording: ON", flush=True)

    def stop_recording(self):
        with self._lock:
            if not self._capturing:
                return
            self._capturing = False
            audio = np.concatenate(self._frames) if self._frames else np.zeros(0, dtype=np.float32)
            self._frames = []
        print(f"[voice-sttd] recording: OFF ({len(audio)/SAMPLE_RATE:.2f}s) — queued", flush=True)
        # Hand off to the background transcription worker and return fast.
        # This keeps ctrl handler threads non-blocking so a rapid press/
        # release/press sequence never loses audio due to the second press
        # waiting on the first utterance's Whisper decode.
        try:
            self._transcribe_queue.put_nowait(audio)
        except queue.Full:
            # Unusual — means the worker is pathologically behind. Drop
            # the oldest pending segment to make room for the new one so
            # we prefer recent speech to stale.
            try:
                self._transcribe_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._transcribe_queue.put_nowait(audio)
            except queue.Full:
                print("[voice-sttd] transcription queue full, dropping audio", file=sys.stderr, flush=True)

    def _transcribe_worker(self) -> None:
        while True:
            audio = self._transcribe_queue.get()
            try:
                self._transcribe_and_broadcast(audio)
            except Exception as e:
                # Never let a single bad transcription kill the worker —
                # log and keep draining.
                print(f"[voice-sttd] transcription error: {e}", file=sys.stderr, flush=True)
            finally:
                self._transcribe_queue.task_done()

    def _transcribe_and_broadcast(self, audio: np.ndarray) -> None:
        if audio.size < SAMPLE_RATE // 4:  # <0.25s, skip
            print("[voice-sttd] audio too short, skipping", flush=True)
            return
        segments, info = self.model.transcribe(
            audio,
            language="en",
            vad_filter=True,
            beam_size=5,
        )
        # Concatenate all segments from one recording into a single broadcast
        # line. In PTT mode, one button press is one thought — splitting a
        # long utterance across multiple channel events makes Claude Code see
        # it as N separate user inputs. Joining here keeps the whole
        # utterance as a single line delivered to every consumer.
        #
        # We join raw segment text (not seg.text.strip()) so Whisper's own
        # tokenizer spacing is preserved — most English segments already
        # start with a leading space.
        text = "".join(seg.text for seg in segments).strip()
        if not text:
            return
        print(f"[voice-sttd] > {text}", flush=True)
        self._broadcast(text + "\n")

    # ---- output socket pubsub ----

    def _broadcast(self, msg: str) -> None:
        data = msg.encode("utf-8")
        # Snapshot the client list under the lock, then sendall() outside
        # it. Holding _out_lock across a blocking sendall would let a
        # single slow consumer wedge every other consumer AND block the
        # accept loop from registering new clients. Dead sockets are
        # collected and removed under the lock in a second pass.
        with self._out_lock:
            clients = list(self._out_clients)

        dead: list[socket.socket] = []
        for c in clients:
            try:
                c.sendall(data)
            except OSError:
                dead.append(c)

        if dead:
            with self._out_lock:
                for c in dead:
                    try:
                        self._out_clients.remove(c)
                    except ValueError:
                        pass  # already removed by a concurrent broadcast
            for c in dead:
                try:
                    c.close()
                except OSError:
                    pass

    def _out_accept_loop(self, sock: socket.socket):
        while True:
            client, _ = sock.accept()
            with self._out_lock:
                self._out_clients.append(client)
            print(f"[voice-sttd] output client connected ({len(self._out_clients)} total)", flush=True)

    # ---- control socket ----

    def _handle_ctrl(self, conn: socket.socket) -> None:
        try:
            data = conn.recv(64).decode("utf-8", errors="ignore").strip()
            if data == "start":
                self.start_recording()
                conn.sendall(b"ok\n")
            elif data == "stop":
                self.stop_recording()
                conn.sendall(b"ok\n")
            else:
                conn.sendall(b"unknown\n")
        finally:
            conn.close()

    def serve(self):
        # Create the socket parent directory with 0700 perms so sockets
        # inside are only reachable by this user. Use umask to guarantee
        # the sockets themselves are bound with 0600-style perms — this
        # closes the bind→chmod race window where the socket inherited
        # umask-derived perms between bind() and chmod().
        os.makedirs(SOCKET_DIR, mode=0o700, exist_ok=True)
        # If someone else (an earlier run, a manual mkdir) created it
        # with looser perms, tighten them.
        try:
            os.chmod(SOCKET_DIR, 0o700)
        except OSError as e:
            print(f"[voice-sttd] warning: could not chmod {SOCKET_DIR}: {e}", file=sys.stderr, flush=True)

        for path in (CTRL_SOCK, OUT_SOCK):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

        prev_umask = os.umask(0o077)
        try:
            out = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            out.bind(OUT_SOCK)
            out.listen(16)
            os.chmod(OUT_SOCK, 0o600)
            threading.Thread(target=self._out_accept_loop, args=(out,), daemon=True).start()

            ctrl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ctrl.bind(CTRL_SOCK)
            ctrl.listen(16)
            os.chmod(CTRL_SOCK, 0o600)
        finally:
            os.umask(prev_umask)

        print(f"[voice-sttd] listening: ctrl={CTRL_SOCK} out={OUT_SOCK}", flush=True)

        while True:
            conn, _ = ctrl.accept()
            threading.Thread(target=self._handle_ctrl, args=(conn,), daemon=True).start()


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
    here so the daemon can be configured entirely through .env — both
    under voice-stt-svc (which used to do its own bash-side bridge) and
    under systemd user units (whose EnvironmentFile doesn't run shell
    logic).
    """
    if "PULSE_SOURCE" in os.environ:
        return
    ps = os.environ.get("VOICE_STT_PULSE_SOURCE", "").strip()
    if ps:
        os.environ["PULSE_SOURCE"] = ps


def main():
    # CLI args default to env vars (populated from .env via voice-stt-svc
    # or a systemd user-unit EnvironmentFile), which in turn fall back to
    # hardcoded defaults. Precedence, highest to lowest:
    #   CLI arg > shell env > .env file > builtin default.
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
    args = p.parse_args()

    input_device = _parse_input_device(args.input_device if args.input_device != "default" else None)
    d = Daemon(args.model, args.device, args.compute_type, input_device)
    try:
        d.serve()
    except KeyboardInterrupt:
        print("\n[voice-sttd] bye", flush=True)


if __name__ == "__main__":
    main()
