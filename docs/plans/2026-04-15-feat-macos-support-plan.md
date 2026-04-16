---
title: "feat: Add macOS support with push-to-toggle dictation"
type: feat
status: completed
date: 2026-04-15
origin: docs/brainstorms/2026-04-15-macos-support-brainstorm.md
---

# feat: Add macOS support with push-to-toggle dictation

## Overview

Port the voice-stt Claude Code channel plugin to macOS. On Mac, dictation uses push-to-toggle: a global keyboard shortcut sends a `toggle` command to the daemon via Unix socket, starting or stopping recording. This replaces Linux's hold-to-talk (evdev) which requires key release events that macOS global shortcuts cannot provide (see brainstorm: docs/brainstorms/2026-04-15-macos-support-brainstorm.md).

## Problem Statement

The dictate plugin is the only Claude Code voice input that works globally (not just when the terminal is focused). It's Linux-only because it depends on evdev for PTT, CUDA for GPU inference, and PulseAudio for audio routing. All three have macOS equivalents that require no new third-party dependencies.

## Proposed Solution

1. Guard Linux-specific code behind `sys.platform` checks
2. Add a Unix domain socket to the daemon for external IPC (`toggle`/`start`/`stop`)
3. Add a `voice-stt` CLI entry point that sends commands to the socket
4. Add audio feedback (beep) on toggle-on/toggle-off
5. Make dependencies conditional in pyproject.toml
6. Update the install skill with macOS-specific paths

The user configures a macOS global keyboard shortcut (System Settings, Raycast, Alfred, etc.) to run `voice-stt toggle`. No Accessibility permission, no extra software, no keylogger-style event taps.

## Implementation Phases

### Phase 1: Platform guards (make it not crash on macOS)

No new features — just make the existing daemon importable and runnable on macOS without errors.

#### 1a. Guard CUDA preload

**File:** `plugin/src/voice_stt/daemon.py` lines 36-53

Wrap the call on line 53 in a platform check:

```python
if sys.platform == "linux":
    _preload_cuda_libs()
```

The function definition itself can stay — it's harmless if never called.

#### 1b. Conditional evdev import

**File:** `plugin/src/voice_stt/daemon.py` line 55

Move `import evdev` from top-level to inside the functions that use it (`_find_keyboards`, `Daemon.__init__` PTT branch, `_ptt_worker`). Or guard the top-level import:

```python
if sys.platform == "linux":
    import evdev
```

The PTT thread creation in `__init__` (lines 160-168) already skips when `_ptt_trigger is None`. On macOS, `ptt_key` will be `None` (empty string default → None on line 462), so the evdev code path is never entered.

#### 1c. Platform-aware argparse defaults

**File:** `plugin/src/voice_stt/daemon.py` lines 418-462

Change hardcoded defaults to platform-aware:

| Arg | Linux default | macOS default | Line |
|-----|---------------|---------------|------|
| `--device` | `cuda` | `cpu` | 425 |
| `--compute-type` | `float16` | `int8` | 429 |
| `--input-device` | `pulse` | `default` | 436 |
| `--ptt-key` | `KEY_F20` | `""` (disabled) | 447 |

Pattern: `os.environ.get("VOICE_STT_COMPUTE_DEVICE", "cuda" if sys.platform == "linux" else "cpu")`

The user's config file overrides these via `config.load()` which calls `os.environ.setdefault()` before argparse runs.

#### 1d. Conditional dependencies

**File:** `plugin/pyproject.toml` lines 8-15

```toml
dependencies = [
    "faster-whisper>=1.0.3",
    "sounddevice>=0.4.7",
    "numpy>=1.26",
    'evdev>=1.6; sys_platform == "linux"',
    'nvidia-cublas-cu12; sys_platform == "linux"',
    'nvidia-cudnn-cu12>=9; sys_platform == "linux"',
]
```

#### 1e. macOS-aware runtime directory

**File:** `plugin/src/voice_stt/singleton.py` line 39

```python
# Current:
runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
# New:
runtime = os.environ.get("XDG_RUNTIME_DIR") or (
    os.environ.get("TMPDIR", "/tmp") if sys.platform == "darwin" else "/tmp"
)
```

Extract this into a shared `runtime_dir()` function (used by both singleton lock and the socket server in Phase 2).

**Checkpoint:** After Phase 1, the daemon starts on macOS, loads the model, opens the mic via CoreAudio, and transcribes audio when receiving `start`/`stop` on stdin. No PTT thread runs. No socket yet — stdin-only control.

---

### Phase 2: Toggle IPC (the macOS PTT mechanism)

#### 2a. Unix domain socket server

**New file:** `plugin/src/voice_stt/socket_server.py`

A lightweight socket server that runs in a daemon thread. Listens on `<runtime_dir>/voice-stt/daemon.sock`. Accepts connections, reads one line-delimited command (`start`, `stop`, `toggle`), dispatches to the `Daemon` instance, closes the connection.

```python
class SocketServer:
    def __init__(self, daemon: Daemon, sock_path: str):
        ...

    def start(self) -> None:
        """Start the socket listener thread."""
        ...

    def shutdown(self) -> None:
        """Clean up the socket file."""
        ...
```

The socket file is cleaned up in `Daemon.shutdown()` (and on startup if stale — check via `connect()` failure then `unlink()`).

#### 2b. Add `toggle` command to daemon

**File:** `plugin/src/voice_stt/daemon.py` lines 333-353

Add to `run_stdin_loop` and to the socket command dispatch:

```python
elif cmd == "toggle":
    if self._capturing:
        self.stop_recording()
    else:
        self.start_recording()
```

Note: `start_recording()` and `stop_recording()` each acquire `self._lock` internally. The toggle must NOT hold the lock when calling them or it will deadlock.

Add a max recording duration safety (configurable, default 120s). A timer thread auto-stops if the user forgets to toggle off.

#### 2c. Audio feedback on toggle

Play a short beep on toggle-on and a different tone on toggle-off. Use `sounddevice.play()` with a generated sine wave (no audio files needed). ~100ms, 880Hz for start, 440Hz for stop. Keep it minimal — one function, inline wave generation with numpy.

#### 2d. CLI entry point

**New file:** `plugin/src/voice_stt/cli.py`

```python
def main():
    """Send a command to the running daemon via Unix socket."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    sock_path = f"{runtime_dir()}/voice-stt/daemon.sock"
    # connect, send cmd + "\n", close
```

**File:** `plugin/pyproject.toml` — add entry point:

```toml
[project.scripts]
voice-sttd = "voice_stt.daemon:main"
voice-stt = "voice_stt.cli:main"
```

Usage: `voice-stt toggle` (or just `voice-stt` — toggle is the default).

**`$PATH` consideration:** The `voice-stt` script is installed into the plugin's `.venv/bin/`, which is not on the user's `$PATH`. The macOS shortcut must use the full path. The install skill should output the exact path after installation (e.g., `~/.claude/plugins/cache/maxinertia/dictate/0.1.0/.venv/bin/voice-stt`) and suggest the user either use the full path in their shortcut, or symlink to `~/bin/` or `/usr/local/bin/`.

#### 2e. Start socket server in daemon

**File:** `plugin/src/voice_stt/daemon.py` in `Daemon.__init__` or `main()`

Start the socket server unconditionally (useful on both platforms — Linux users could also use toggle mode if they want). Clean up in `Daemon.shutdown()`.

The channel server (voice-stt-channel.ts) continues using stdin to communicate with the daemon — no changes needed there. The socket is a supplementary IPC channel for external processes (keyboard shortcuts, CLI tools). Both stdin and socket can coexist; the daemon already handles concurrent `start`/`stop` calls safely via `self._lock`.

**Checkpoint:** After Phase 2, a macOS user can run the daemon, then run `voice-stt toggle` from a keyboard shortcut to start/stop recording with audio feedback.

---

### Phase 3: Install skill and config

#### 3a. Platform-conditional install skill

**File:** `plugin/commands/install.md`

Add a platform detection preamble:

```bash
uname -s  # "Linux" or "Darwin"
```

Then branch each step:

- **Prereqs (step 1):** On macOS, skip `input` group, `libportaudio2`, `nvidia-smi` checks. Add `uname -m` check for `arm64` (Apple Silicon) vs `x86_64` (Intel) to inform default compute device.
- **Config (step 4):** On macOS, create config with macOS defaults: `cpu`, `int8`, `default` input device, `toggle` PTT mode, no PTT key.
- **Legacy migration (steps 5-6):** Skip entirely on macOS (no systemd, no `.env` migration).
- **Next steps (step 8):** On macOS, guide user to set up a global keyboard shortcut for `voice-stt toggle` instead of evdev/input-remapper instructions.

#### 3b. Config template updates

**File:** `plugin/config.template`

Add a new PTT mode setting and document macOS defaults in comments:

```bash
# Max recording duration in seconds (safety for toggle mode).
# Auto-stops recording if user forgets to toggle off.
# Set to 0 to disable. Only applies when PTT key is unset.
VOICE_STT_TOGGLE_MAX_DURATION=120
```

#### 3c. PTT mode selection

No separate `PTT_MODE` config key needed. The existing `--ptt-key` already controls this: when `--ptt-key` is empty (the macOS default), no evdev PTT thread starts, and the daemon operates in toggle mode via the socket. When `--ptt-key` is set (the Linux default, `KEY_F20`), the evdev hold-to-talk thread starts as before. One knob, not two.

---

## Acceptance Criteria

- [ ] `uv sync` succeeds on macOS (no evdev/nvidia install failures)
- [ ] `voice-sttd` starts on macOS: loads model on CPU, opens mic via CoreAudio
- [ ] `voice-stt toggle` starts recording (with beep), second invocation stops and transcribes (with different beep)
- [ ] Transcripts appear in the Claude Code session as `<channel source="dictate">` events
- [ ] Auto-stop fires after configurable timeout if user forgets to toggle off
- [ ] All existing Linux behavior is unchanged (hold-to-talk via evdev, CUDA, PulseAudio)
- [ ] `/dictate:install` skill detects macOS and guides user through macOS-specific setup
- [ ] No Accessibility permission required on macOS
- [ ] Works on M3 Mac with macOS 26.4.1

## Technical Considerations

- **Audio capture needs no changes** — `sounddevice` auto-selects CoreAudio on macOS (see brainstorm)
- **`fcntl.flock` works on macOS** — BSD heritage, no portability issue
- **Socket cleanup on crash** — if daemon crashes, stale socket file remains. On next startup, attempt `connect()` to stale socket; if it fails, `unlink()` and recreate. This is standard Unix socket practice.
- **Toggle beep and recording overlap** — play the beep on the output device, not the input device. The beep should not be captured in the recording. Use `sounddevice.play()` which uses the default output device.
- **Model download on first run** — `faster-whisper` downloads the model on first use. This happens identically on both platforms. The install skill should mention the ~1GB download for `medium.en`.

## Dependencies & Risks

- **Risk: CoreAudio device naming** — macOS audio device names differ from Linux. The default `input_device=None` (system default mic) should work out of the box. Document how to list devices with `python3 -m sounddevice` if the user needs a specific mic.
- **Risk: CTranslate2 on Apple Silicon** — faster-whisper uses CTranslate2 which supports CPU on all platforms. If CPU latency is too high on M3, Metal/MPS support exists in CTranslate2 but is a separate follow-up.
- **Risk: `uv` availability on macOS** — `uv` is available via Homebrew (`brew install uv`) and via the official installer. The install skill should check and guide.
- **No risk: Bun on macOS** — Bun has first-class macOS support.

## Sources & References

- **Origin brainstorm:** [docs/brainstorms/2026-04-15-macos-support-brainstorm.md](docs/brainstorms/2026-04-15-macos-support-brainstorm.md) — Key decisions: push-to-toggle over hold-to-talk; Unix socket IPC; no event taps/keyloggers; CPU-only initially; audio beep feedback; keep `~/.config/` paths on both platforms
- **Original channel architecture:** [docs/plans/2026-04-11-feat-voice-stt-claude-code-channel-plan.md](docs/plans/2026-04-11-feat-voice-stt-claude-code-channel-plan.md)
- **Daemon refactor plan:** [docs/plans/2026-04-12-refactor-collapse-daemon-into-plugin-plan.md](docs/plans/2026-04-12-refactor-collapse-daemon-into-plugin-plan.md)
- **Obsidian vault docs:** `~/obsidian-vault/docs/razer-naga-button-remapping.md`, `~/obsidian-vault/docs/voice-stt-claude-code-setup.md`
