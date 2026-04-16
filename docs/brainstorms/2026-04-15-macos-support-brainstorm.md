# Brainstorm: macOS Support for voice-stt (dictate plugin)

**Date:** 2026-04-15
**Status:** Complete
**Target:** M3 Mac, macOS 26.4.1

## What We're Building

macOS support for the voice-stt Claude Code channel plugin. Press a keyboard shortcut to start recording, press again to stop. Transcript appears in the Claude Code session regardless of which window is focused.

## Why This Approach

### The problem with existing alternatives

- Claude Code's built-in `/voice` requires terminal focus — can't dictate from another window
- Every third-party voice plugin on GitHub has the same limitation (terminal-focused)
- The dictate plugin is the only one with global PTT, but it's Linux-only (evdev)

### macOS constraints

- **No key release events** in macOS global shortcuts — rules out hold-to-talk without extra software
- **Event taps / pynput** act as system-wide keyloggers, require Accessibility permission — unacceptable
- **No openrazer on macOS** — Naga side buttons can't be put into keyboard mode

### Push-to-toggle solves it

macOS global shortcuts fire on key-down, which is all toggle needs. No release event, no HID device reading, no Accessibility permission, no extra hardware. A global shortcut runs a small CLI command that tells the daemon to toggle recording on/off.

This is a behavior change from Linux (hold-to-talk) but the right trade-off for macOS: zero dependencies, zero permissions, works with any keyboard shortcut the user configures.

## Key Decisions

### PTT mode: toggle (macOS) vs hold-to-talk (Linux)

**Decision:** Push-to-toggle on macOS. Hold-to-talk stays on Linux (evdev).

The daemon already has `start` and `stop` commands on stdin. Add a `toggle` command that flips between the two. Expose it via a Unix socket or a CLI wrapper so a macOS global shortcut can invoke it:

```bash
# macOS shortcut runs:
voice-stt toggle
# or:
echo toggle | nc -U ~/.local/share/voice-stt/daemon.sock
```

The user configures their preferred shortcut in macOS System Settings → Keyboard → Keyboard Shortcuts → App Shortcuts, or via Raycast/Alfred/Hammerspoon.

**Safety:** Add a configurable max recording duration (e.g., 60s) that auto-stops if the user forgets to press again.

### Toggle IPC mechanism

**Decision:** Unix domain socket. The daemon listens on `$XDG_RUNTIME_DIR/voice-stt/daemon.sock` (Linux) or `$TMPDIR/voice-stt/daemon.sock` (macOS). Accepts line-delimited commands: `start`, `stop`, `toggle`.

Why a socket instead of stdin:
- stdin is owned by the channel plugin (voice-stt-channel.ts pipes commands through it)
- A socket allows external commands from any process (keyboard shortcut, CLI tool, script)
- Works on both Linux and macOS
- The channel plugin can also use the socket instead of stdin (simplification)

A thin CLI wrapper (`voice-stt toggle`) connects to the socket, sends `toggle\n`, and exits. This is what the keyboard shortcut invokes.

### Audio capture

**Decision:** No code changes. `sounddevice` auto-selects CoreAudio on macOS.

Config differences:
- `VOICE_STT_INPUT_DEVICE`: device name or index (same API, different device names)
- `VOICE_STT_PULSE_SOURCE`: ignored on macOS (no PulseAudio)

### GPU inference

**Decision:** CPU-only on macOS initially.

- Default `VOICE_STT_COMPUTE_DEVICE=cpu` on macOS
- Default `VOICE_STT_COMPUTE_TYPE=int8` (fast on M3 CPU, lower memory)
- `medium.en` model should be responsive enough — CTranslate2 is well-optimized for CPU
- Metal/MPS backend can be a follow-up if latency is a problem

### CUDA preload guard

**Decision:** Wrap `_preload_cuda_libs()` in `sys.platform == 'linux'`. Currently runs unconditionally at module import and would crash on macOS (no nvidia .so files).

### Dependencies

**Decision:** Conditional deps in pyproject.toml.

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

No new macOS-specific deps. The Unix socket uses stdlib only.

### Config and paths

**Decision:** Keep `~/.config/voice-stt/config` on both platforms.

New config key:
```
# PTT mode: "hold" (Linux default, requires evdev) or "toggle" (macOS default)
VOICE_STT_PTT_MODE=toggle
```

On macOS, default to `toggle` and skip the evdev PTT thread entirely. On Linux, default to `hold` (existing behavior). User can override on either platform.

### Install skill

**Decision:** Platform-conditional sections in install.md.

macOS install:
- Check for `uv` and `bun`
- Skip `input` group, `libportaudio2`, and NVIDIA checks
- Detect M-series vs Intel for compute device default
- Create config file with macOS defaults (`cpu`, `int8`, `toggle`)
- Guide user to set up a global keyboard shortcut for `voice-stt toggle`

## Resolved Questions

1. **Toggle feedback:** Short beep on toggle-on and toggle-off (distinct tones so you know which). On Linux hold-to-talk the physical button provides feedback; toggle needs an audio substitute.

2. **Socket location on macOS:** `$TMPDIR` is per-user on macOS (`/var/folders/xx/...`), private enough. Use it.

3. **Metal/MPS:** Implement Metal support alongside CPU. Test on the M3 Mac once the port is done.
