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

from . import CTRL_SOCK, OUT_SOCK, SAMPLE_RATE


class Daemon:
    def __init__(self, model_name: str, device: str, compute_type: str):
        from faster_whisper import WhisperModel  # imported lazily so --help is fast

        print(f"[voice-sttd] loading model={model_name} device={device} compute={compute_type}", flush=True)
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        print("[voice-sttd] model ready", flush=True)

        self._lock = threading.Lock()
        self._recording = False
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None

        self._out_clients: list[socket.socket] = []
        self._out_lock = threading.Lock()

    # ---- audio capture ----

    def _audio_cb(self, indata, frames, time, status):
        if status:
            print(f"[voice-sttd] audio status: {status}", file=sys.stderr, flush=True)
        # indata is float32 shape (frames, channels); we requested mono
        self._frames.append(indata[:, 0].copy())

    def start_recording(self):
        with self._lock:
            if self._recording:
                return
            self._frames = []
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=self._audio_cb,
            )
            self._stream.start()
            self._recording = True
            print("[voice-sttd] recording: ON", flush=True)

    def stop_recording(self):
        with self._lock:
            if not self._recording:
                return
            assert self._stream is not None
            self._stream.stop()
            self._stream.close()
            self._stream = None
            self._recording = False
            audio = np.concatenate(self._frames) if self._frames else np.zeros(0, dtype=np.float32)
            self._frames = []
        print(f"[voice-sttd] recording: OFF ({len(audio)/SAMPLE_RATE:.2f}s) — transcribing", flush=True)
        self._transcribe_and_broadcast(audio)

    def _transcribe_and_broadcast(self, audio: np.ndarray):
        if audio.size < SAMPLE_RATE // 4:  # <0.25s, skip
            print("[voice-sttd] audio too short, skipping", flush=True)
            return
        segments, info = self.model.transcribe(
            audio,
            language="en",
            vad_filter=True,
            beam_size=5,
        )
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            print(f"[voice-sttd] > {text}", flush=True)
            self._broadcast(text + "\n")

    # ---- output socket pubsub ----

    def _broadcast(self, msg: str):
        data = msg.encode("utf-8")
        dead: list[socket.socket] = []
        with self._out_lock:
            for c in self._out_clients:
                try:
                    c.sendall(data)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._out_clients.remove(c)
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

    def _handle_ctrl(self, conn: socket.socket):
        try:
            data = conn.recv(64).decode("utf-8", errors="ignore").strip()
            if data == "start":
                self.start_recording()
                conn.sendall(b"ok\n")
            elif data == "stop":
                self.stop_recording()
                conn.sendall(b"ok\n")
            elif data == "toggle":
                if self._recording:
                    self.stop_recording()
                else:
                    self.start_recording()
                conn.sendall(b"ok\n")
            elif data == "status":
                conn.sendall(b"recording\n" if self._recording else b"idle\n")
            elif data == "quit":
                conn.sendall(b"bye\n")
                os._exit(0)
            else:
                conn.sendall(b"unknown\n")
        finally:
            conn.close()

    def serve(self):
        for path in (CTRL_SOCK, OUT_SOCK):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

        out = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        out.bind(OUT_SOCK)
        out.listen(16)
        os.chmod(OUT_SOCK, 0o600)
        threading.Thread(target=self._out_accept_loop, args=(out,), daemon=True).start()

        ctrl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ctrl.bind(CTRL_SOCK)
        ctrl.listen(16)
        os.chmod(CTRL_SOCK, 0o600)
        print(f"[voice-sttd] listening: ctrl={CTRL_SOCK} out={OUT_SOCK}", flush=True)

        while True:
            conn, _ = ctrl.accept()
            threading.Thread(target=self._handle_ctrl, args=(conn,), daemon=True).start()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="medium.en", help="faster-whisper model name (e.g. small.en, medium.en, large-v3)")
    p.add_argument("--device", default="cuda")
    p.add_argument("--compute-type", default="float16", help="float16 (GPU), int8_float16, int8 (CPU)")
    args = p.parse_args()

    d = Daemon(args.model, args.device, args.compute_type)
    try:
        d.serve()
    except KeyboardInterrupt:
        print("\n[voice-sttd] bye", flush=True)


if __name__ == "__main__":
    main()
