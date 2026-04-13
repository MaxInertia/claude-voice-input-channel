---
title: "refactor: collapse daemon into plugin, drop systemd, single-command install"
type: refactor
status: active
date: 2026-04-12
---

# Collapse voice-sttd into the Claude Code plugin

## Overview

Delete the separate `voice-sttd` daemon + `voice-stt-ptt` listener + systemd user units + `voice-stt-svc` bash supervisor + `claude-voice` launcher. Replace them with a single Python subprocess spawned by the TypeScript channel MCP server, whose lifetime is tied to the Claude Code session. Move user config from `$PROJECT_DIR/.env` to `$XDG_CONFIG_HOME/voice-stt/config`, created on first run by a new `/voice-stt:install` slash command.

After the refactor, install looks exactly like voicemode's flow:

```
/plugin marketplace add MaxInertia/claude-voice-input-channel
/plugin install voice-stt@voice-stt-local
/voice-stt:install
```

No shell, no systemctl, no git clone. And the net diff is **strongly negative** — we delete ~500 lines of bash/systemd infrastructure in exchange for ~200 lines of TS subprocess orchestration and a Python XDG config loader.

## Problem statement

The current architecture was built on a false assumption: that multiple Claude Code sessions might want to share one warm Whisper model. That assumption is wrong for this use case — there's one mic, one PTT button, one user. Nobody runs two Claude Code sessions both listening for voice input simultaneously. But the architecture we built to support that hypothetical scenario has a real installation cost:

- `scripts/systemd/voice-sttd.service.in` + `voice-stt-ptt.service.in` (systemd unit templates with `@VOICE_STT_ROOT@` substitution)
- `scripts/install-systemd-units` (template renderer + systemctl daemon-reload wrapper)
- `scripts/voice-stt-svc` (bash wrapper that forwards to systemctl when units are installed, falls back to pgrep-based supervisor otherwise — 160 lines, two code paths, one known pgrep self-match bug in the legacy path)
- `scripts/claude-voice` (launcher that checks daemon health via systemctl and execs Claude Code with the dev-channels flag)
- `src/voice_stt/daemon.py::_bridge_pulse_source()` + `src/voice_stt/daemon.py::Daemon._handle_ctrl` + `Daemon._out_accept_loop` + `Daemon._broadcast` (unix-socket ctrl/out protocol that only exists because the daemon is a separate process)
- `.env.example` at repo root, `.gitignore` rules for `.env`/`.env.*`, `.env`-parsing code in `voice-stt-svc`, `.env` permission-tightening code, systemd `EnvironmentFile=` directive, documentation split across the README's Run section and the legacy subsection
- `src/voice_stt/ptt_listener.py` as a separate process that has its own rescan loop and ctrl-socket client because it doesn't share address space with the daemon
- Users have to clone the repo, run `uv sync`, run `install-systemd-units`, `systemctl --user enable --now`, then remember a wrapper to launch Claude Code with the right flags. Five steps before they can say "Hello."

What we actually need is: when Claude Code starts the channel plugin, a Python process comes up, loads Whisper, captures mic audio, listens for the PTT key, and forwards transcripts to the MCP channel. When Claude Code exits, that process dies. One install step. One process tree. One config file in one place.

## Proposed solution

Collapse the daemon into the plugin. The TypeScript channel MCP server (`plugin/channel/voice-stt-channel.ts`) spawns a Python subprocess running the daemon code, reads transcript lines from its stdout, and forwards them as `notifications/claude/channel` events. When the TS channel exits (Claude Code session ends), the Python child detects EOF on its stdin and cleanly terminates. No sockets, no systemd, no wrapper scripts.

Config lives at `$XDG_CONFIG_HOME/voice-stt/config` (typically `~/.config/voice-stt/config`) — outside the plugin cache so it survives plugin updates, outside the repo so it's never in git. A `/voice-stt:install` slash command handles first-run setup: prereq checks, `uv sync`, `bun install`, creating the config file from a bundled template, migration from any existing `$PROJECT_DIR/.env`.

Two processes, one install command, one config file, one lifecycle.

## Technical approach

### Architecture before

```
user starts systemd services manually
    │
    ▼
voice-sttd.service (systemd user unit)
    ├─ uv run voice-sttd (Python)
    │    ├─ WhisperModel in CUDA
    │    ├─ persistent sd.InputStream
    │    ├─ serves ctrl.sock (start/stop commands)
    │    └─ broadcasts to out.sock
    │
voice-stt-ptt.service (systemd user unit)
    └─ uv run voice-stt-ptt (Python)
         ├─ evdev KEY_F20 listener
         └─ connects to ctrl.sock per keypress

user starts Claude Code via `claude-voice` wrapper
    │
    ▼
claude-voice (bash)
    ├─ systemctl --user is-active voice-sttd.service  # health check
    └─ exec claude --dangerously-load-development-channels plugin:voice-stt@voice-stt-local
         │
         ▼
         Claude Code spawns voice-stt-channel.ts (Bun TS)
              └─ connects to out.sock, forwards to Claude as <channel>
```

Four long-lived processes (daemon, ptt, Bun channel server, Claude Code), two unix sockets, one bash wrapper with health check.

### Architecture after

```
user starts Claude Code (any way they already do)
    │
    ▼
Claude Code spawns voice-stt-channel.ts (Bun TS, via plugin MCP config)
    │    stdio: MCP protocol ↔ Claude Code session
    │
    │    Bun.spawn()
    ▼
voice-sttd (Python child, parent = channel.ts)
    ├─ loads $XDG_CONFIG_HOME/voice-stt/config at startup
    ├─ WhisperModel in CUDA
    ├─ persistent sd.InputStream
    ├─ evdev KEY_F20 listener thread
    ├─ transcription worker thread
    │
    ▲    line-delimited transcripts on stdout
    │
TS channel reads child stdout, forwards as notifications/claude/channel
```

Two processes (Bun channel + Python child), stdio pipe between them, no sockets, no systemd, no separate PTT listener.

### Implementation phases

Each phase is a commit (or small commit set) that leaves the tree working. Phases can be merged incrementally or all at once.

#### Phase 1 — Python daemon speaks stdio instead of sockets

Refactor `src/voice_stt/daemon.py` so commands arrive via stdin and transcripts go to stdout:

- **Input protocol (stdin, line-delimited):** `start\n` / `stop\n`. On EOF (parent closed stdin), clean up and exit. The daemon reads its own stdin in a `threading.Thread(target=_stdin_loop, daemon=True)` so it doesn't block the audio callback thread or the main thread.
- **Output protocol (stdout, line-delimited):** one UTF-8 transcript per line, terminated by `\n`. No framing headers, no JSON envelope — keeps the TS parser trivial. This exactly matches the current `_broadcast` format.
- **Log protocol (stderr):** everything else. Model load, `recording: ON/OFF` lines, errors, PULSE_SOURCE debug. The TS parent forwards stderr directly to Claude Code's debug log so it shows up in `~/.claude/debug/`.

Delete from daemon.py: `_handle_ctrl`, `_out_accept_loop`, `_broadcast`, `serve()`'s socket-binding code, `_out_clients`, `_out_lock`, the `SOCKET_DIR` / `CTRL_SOCK` / `OUT_SOCK` imports. Replace `serve()` with a new `main loop` that reads stdin lines and drives `start_recording()` / `stop_recording()`.

Delete from `src/voice_stt/__init__.py`: `SOCKET_DIR`, `CTRL_SOCK`, `OUT_SOCK`. Keep `SAMPLE_RATE`.

**Test:** `uv run voice-sttd < /dev/tty` in a terminal. Type `start<enter>`, speak, type `stop<enter>`. Verify a transcript appears on stdout. Ctrl-D should cause the daemon to exit cleanly.

#### Phase 2 — PTT listener becomes a thread inside voice-sttd

Delete `src/voice_stt/ptt_listener.py` as a separate entrypoint. Move its functionality into `src/voice_stt/ptt_thread.py` (or fold into `daemon.py`) as a `threading.Thread` that runs alongside the audio callback.

- The thread watches evdev for the configured `KEY_F20` press/release edges (same rescan-on-disappearance loop from the review-fixes branch — `5a3f391`).
- On press, it calls `daemon.start_recording()` directly. On release, `daemon.stop_recording()`. No IPC — these are method calls within the same Python process.
- The thread exits when the main daemon exits (daemon=True so it doesn't block shutdown).

Delete `voice-stt-ptt` entrypoint from `pyproject.toml::[project.scripts]`.

**Test:** Launch the daemon in a terminal. Press the PTT key. Verify a transcript appears on stdout without having typed any stdin commands.

##### Tradeoff: one process for transcription + PTT — is that OK?

Fair question, worth addressing explicitly. The risks of collapsing two independent Linux processes into one Python process with multiple threads:

1. **GIL contention.** The PTT thread is a tight loop on `select()` over evdev fds; Whisper inference is a heavy CUDA workload; the audio callback thread buffers mic frames. Does the PTT thread stay responsive while Whisper is busy? **Yes.** `faster-whisper` delegates to CTranslate2, which releases the GIL during its C++ inference loop — that's explicitly documented. `sounddevice`'s PortAudio callback also runs in a C thread that doesn't hold the GIL during audio I/O. The PTT `select()` call itself releases the GIL. Net result: all three threads operate independently at the C level; the GIL is only contended during trivial Python-level transitions (appending a numpy array to `_frames`, setting `_capturing = True`, incrementing a seq counter). Worst case you add ~1ms of jitter to PTT press detection — imperceptible.

2. **Crash blast radius.** In the current three-process architecture, a Whisper segfault kills `voice-sttd` but leaves `voice-stt-ptt` running. In the collapsed architecture, a Whisper segfault kills everything. **Does isolation matter?** Only if the surviving PTT listener can do anything useful while Whisper is dead — and it can't (nothing to talk to). The TS channel's auto-restart loop from Phase 4 re-spawns the whole Python child within 2-3 seconds, so the user sees the same outcome either way: a brief gap, then dictation works again. The "independent lifecycle" isolation was cosmetic — we never actually exploited it.

3. **Harder to debug one misbehaving piece.** In separate-process mode, you can `strace -p` one and leave the other running. In collapsed mode, `strace` sees everything mixed together. This is mildly annoying but not blocking; `py-spy dump --pid` gives you per-thread stacks cleanly anyway.

4. **Audio callback re-entrancy with evdev + Whisper.** The PortAudio callback is called from a real-time audio thread; the PTT thread is blocked on `select()`; the transcription worker is blocked on a `queue.Queue.get()`. None of them touch each other's data except via the two shared variables `_capturing` (a bool — atomic writes under the GIL) and `_frames` (a list — only mutated while `_capturing=True`, snapshotted+cleared by stop_recording under `_lock` before transcription reads it). The current three-process code *already* has this exact threading model inside the daemon process — we're just adding evdev read as a fourth thread, which doesn't interact with the other three at all.

**Verdict:** safe. The concern would be real if PTT needed to stay responsive while Whisper was blocking, AND Python held the GIL during inference — neither is the case. One process for everything is strictly simpler and functionally identical.

One thing we DO lose: the ability to `systemctl --user restart voice-sttd.service` without touching the PTT. If Whisper wedges and we want to kill-and-restart just that piece, we have to `kill` the entire Python child and the TS restarts it. In practice we never did per-service surgery anyway.

#### Phase 3 — XDG config loader

Create `src/voice_stt/config.py`:

```python
# src/voice_stt/config.py
"""
Load $XDG_CONFIG_HOME/voice-stt/config into os.environ on startup.

Precedence (highest to lowest):
    CLI arg  >  shell env  >  config file  >  builtin default
"""
import os
from pathlib import Path


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "voice-stt" / "config"


def load() -> None:
    path = config_path()
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key.isidentifier():
            continue
        # shell env wins over config file
        os.environ.setdefault(key, val)
```

Call `config.load()` at the top of `daemon.main()`, before `_bridge_pulse_source()` (which stays) and before argparse. Nothing else changes — the existing env-var fallback chain in argparse defaults just works.

Create `plugin/config.template` with the current `.env.example` content (VOICE_STT_MODEL, VOICE_STT_COMPUTE_DEVICE, VOICE_STT_COMPUTE_TYPE, VOICE_STT_INPUT_DEVICE, VOICE_STT_PULSE_SOURCE, VOICE_STT_PTT_KEY).

**Test:** `cp plugin/config.template ~/.config/voice-stt/config`, set one value, `uv run voice-sttd`, verify daemon honors it.

#### Phase 4 — TS channel spawns the Python daemon as a subprocess

Rewrite `plugin/channel/voice-stt-channel.ts` to own the Python child's lifecycle:

```ts
// sketch of the new channel.ts structure
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'

const mcp = new Server(
  { name: 'voice-stt', version: '0.1.0' },
  { capabilities: { experimental: { 'claude/channel': {} } }, instructions: '...' },
)
await mcp.connect(new StdioServerTransport())

// Spawn the Python daemon. ${CLAUDE_PLUGIN_ROOT} points at the cached
// plugin dir; the pyproject.toml sits there, so `uv run` works.
const pluginRoot = process.env.CLAUDE_PLUGIN_ROOT ?? import.meta.dir + '/..'
const child = Bun.spawn({
  cmd: ['uv', 'run', '--project', pluginRoot, 'voice-sttd'],
  stdin:  'pipe',
  stdout: 'pipe',
  stderr: 'pipe',
  env: { ...process.env },  // forward env including XDG_CONFIG_HOME
})

// Read child stdout line-by-line, forward each transcript as a channel event.
let seq = 0
let buf = Buffer.alloc(0)
for await (const chunk of child.stdout) {
  buf = Buffer.concat([buf, chunk])
  let i: number
  while ((i = buf.indexOf(0x0a)) !== -1) {
    const line = buf.subarray(0, i).toString('utf8').trim()
    buf = buf.subarray(i + 1)
    if (!line) continue
    seq += 1
    await mcp.notification({
      method: 'notifications/claude/channel',
      params: { content: line, meta: { seq: String(seq), ts: String(Date.now()) } },
    })
  }
}

// Forward child stderr to our own stderr so it lands in Claude Code's debug log.
;(async () => {
  for await (const chunk of child.stderr) process.stderr.write(chunk)
})()

// When Claude Code tears down the MCP server, kill the Python child.
process.on('SIGTERM', () => child.kill('SIGTERM'))
process.on('SIGINT',  () => child.kill('SIGTERM'))
```

No more unix sockets. No more reconnect loop (the child is a direct subprocess — if it dies, we restart it with bounded retries, but there's no network latency or "is the daemon up yet" polling). The child detects parent death by watching stdin EOF in its own stdin-reading thread.

**Restart-on-crash:** if `child.exited` fires while the TS channel is still alive, log to stderr and spawn a fresh child. Cap retries at 3 within 30 seconds; after that, log a persistent error and stop restarting (user will see the error in Claude Code's debug log).

##### Single-session enforcement (first-class design element)

Voice-stt should refuse to run a second instance. Two instances means two copies of Whisper loaded into VRAM (~3 GB wasted), two PortAudio mic opens competing for the same device, two PTT listeners both firing on each keypress — none of which makes sense for a one-user-with-one-mic workflow. Enforce singleton semantics with an advisory file lock.

**Where the lock lives.** `$XDG_RUNTIME_DIR/voice-stt/daemon.lock`. Per-user (`$XDG_RUNTIME_DIR` is `/run/user/$UID`), 0700 parent dir by design, automatically cleaned up by tmpfiles when the user's session ends.

**How it's acquired** (inside the Python daemon, not the TS channel — the daemon is the resource owner):

```python
# src/voice_stt/singleton.py
import fcntl
import os
import sys
from pathlib import Path

def _lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    d = Path(runtime) / "voice-stt"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / "daemon.lock"

def acquire_or_exit() -> None:
    """Acquire the singleton lock or exit with a clear message.

    The returned file descriptor is NOT closed — the lock is held
    for the lifetime of the process. When the process dies (even
    SIGKILL), the kernel releases the lock, so there's no stale
    state to clean up.
    """
    path = _lock_path()
    # Open for write + create, don't truncate — the holding pid is
    # written to the file for debuggability but never relied on for
    # liveness (the lock itself is authoritative).
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            existing = path.read_text().strip()
        except OSError:
            existing = "unknown"
        sys.stderr.write(
            f"[voice-sttd] another voice-stt daemon is already running "
            f"(lock held on {path}, previous pid: {existing}).\n"
            f"[voice-sttd] exit the other Claude Code session first, "
            f"or run `kill {existing}` if it's stale.\n"
        )
        sys.exit(2)
    # Record our pid in the file (not load-bearing, just diagnostic).
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    # Intentionally leak `fd` — holding it open keeps the lock held
    # until the process exits, at which point the kernel drops it.
    # Return nothing; no one needs a handle.

# Called first thing in daemon.main(), before model load.
```

**Why `fcntl.flock` with LOCK_EX | LOCK_NB:**
- **Advisory, not mandatory.** No kernel-level enforcement, but since we control every process that opens this file, advisory is enough.
- **Automatically released on process death.** The kernel tracks the holder. `kill -9` releases the lock. No stale lockfiles ever, regardless of how ungracefully the previous process died. This is the failure mode that kills pidfile-based approaches.
- **Fast path on success.** Zero syscalls beyond the one `flock(LOCK_NB)` call.
- **Non-blocking.** `LOCK_NB` means "fail immediately if held" — no waiting around, no deadlock potential.

**User experience when a second session is attempted:**

1. User launches a second Claude Code session with the channel flag (they forgot the first one is running, or tiled-window situation).
2. Claude Code spawns a new `voice-stt-channel.ts` → new `bun` → new Python child.
3. Python child calls `singleton.acquire_or_exit()` first thing in `main()`, before model load.
4. Lock acquisition fails. Python writes the diagnostic message to stderr and exits with code 2.
5. Bun's stdout reader sees EOF on the child; Bun's stderr reader forwards the diagnostic message.
6. The TS channel applies the restart-on-crash policy: retries 3 times in 30 seconds, all fail identically (lock still held by the first session), then gives up and logs a persistent error to Claude Code's debug log.
7. User dictates in the second session — nothing happens. They check `~/.claude/debug/<session>.txt`, see the "another voice-stt daemon is already running" message, realize they had one running elsewhere, and exit the other session.

The only cost: a doomed-to-fail restart loop for 30 seconds in the second session before the error becomes persistent. Mitigation: bypass the retry logic if the first spawn exits with `status == 2` specifically, since that's our sentinel for "permanent lock contention, don't retry."

**Why the lock lives in Python, not in the TS channel server:** the Python daemon is the thing that opens the mic and loads the model. Those are the resources we're protecting. The TS channel is a thin forwarder — two of them could coexist fine. Holding the lock at the Python layer protects the actual scarce resources even if someone ever spawns the daemon outside the plugin flow (e.g., directly from a terminal for debugging).

**What about running the daemon standalone for debugging?** The singleton check applies there too. If you want to run a debug daemon while the plugin-spawned daemon is active, you have to kill the plugin-spawned one first. That's an acceptable constraint — debugging implies you're *not* trying to use the live pipeline simultaneously.

**Test:** Launch Claude Code with the plugin, verify the TS server spawns Python, verify transcripts flow end-to-end. Kill the Python child manually (`pkill -f voice-sttd`), verify it gets restarted. Kill the TS process (`/exit` Claude Code), verify the Python child exits within ~1 second (via stdin EOF). Launch a second Claude Code session while the first is running; verify its Python child exits with code 2 and the "already running" error appears in the second session's debug log within ~2 seconds (before the retry loop even starts, thanks to the exit-code sentinel).

**New file in this phase:** `src/voice_stt/singleton.py` (the flock helper shown above). Called from the very top of `daemon.main()`, before any other initialization.

#### Phase 5 — `/voice-stt:install` slash command

Create `plugin/commands/install.md` — a slash command, which in Claude Code plugin terms is a markdown file whose body is instructions for the agent:

```markdown
---
description: Install voice-stt system dependencies and create the user config file
---

You are setting up voice-stt for the user. Work through these steps and
report the outcome of each. If any step fails, stop and tell the user
what went wrong before continuing.

1. **Prereq check.** Verify these commands exist and report their versions:
   - `uv` (Python package manager)
   - `bun` (TypeScript runtime used by the channel server)
   - `nvidia-smi` (NVIDIA driver — optional, warn if missing and suggest small.en model)
   - `libportaudio2` system package — check with `dpkg -l libportaudio2` on Debian/Ubuntu

   If `uv` is missing, tell the user: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
   If `bun` is missing, tell the user: `curl -fsSL https://bun.sh/install | bash`.
   If `libportaudio2` is missing on Debian/Ubuntu: `sudo apt install libportaudio2`.

2. **Install Python dependencies.** Run:
   ```
   cd "${CLAUDE_PLUGIN_ROOT}" && uv sync
   ```
   Report the output. On first run this downloads ~200 MB of wheels.

3. **Install TypeScript dependencies.** Run:
   ```
   cd "${CLAUDE_PLUGIN_ROOT}/channel" && bun install --frozen-lockfile
   ```

4. **Create the user config file** if it doesn't already exist. The path
   is `$XDG_CONFIG_HOME/voice-stt/config` (defaults to
   `$HOME/.config/voice-stt/config`).

   - If the file does not exist: create the parent directory with
     `mkdir -p`, copy `${CLAUDE_PLUGIN_ROOT}/config.template` to the
     config path, and `chmod 600` it. Tell the user the path and what
     they might want to edit (model size, PULSE_SOURCE for noise-cancel
     bypass, PTT key).
   - If the file already exists: leave it alone, report its path and
     size so the user knows it was preserved.

5. **Migration from legacy .env.** Check for `$HOME/projects/voice-stt/.env`.
   If present AND we just created the user config in step 4, copy the
   contents of the old `.env` into the new config (preserving any
   VOICE_STT_PULSE_SOURCE override the user might have). Tell the user
   their old config was migrated.

6. **Migration from legacy systemd units.** Check for
   `$HOME/.config/systemd/user/voice-sttd.service`. If present, tell the
   user we're cleaning up the old install:
   ```
   systemctl --user disable --now voice-sttd.service voice-stt-ptt.service
   rm ~/.config/systemd/user/voice-sttd.service
   rm ~/.config/systemd/user/voice-stt-ptt.service
   systemctl --user daemon-reload
   ```
   Ask before running if you think the user might want to keep them.

7. **Print next steps.** Tell the user:
   - Voice dictation now runs as part of the Claude Code session itself.
   - Launch Claude Code with the channel active:
     `claude --dangerously-load-development-channels plugin:voice-stt@voice-stt-local`
   - Or alias that command in your shell rc if you want muscle memory.
   - Then press the PTT key (default KEY_F20 — you'll need to remap a
     hardware button to it via input-remapper/xremap/kmonad; see the
     README for details).

8. **Validation.** Run one last check: `ls -la "${HOME}/.config/voice-stt/config"`
   and report that it exists with 0600 permissions.
```

Slash commands in Claude Code are not bash scripts — they're agent-executed markdown. The agent runs each shell command via its Bash tool, captures stdout/stderr, and reports outcomes to the user. This means the install flow gets free error handling, progress reporting, and interactive confirmation for destructive steps.

**Test:** `/voice-stt:install` on a machine with the old setup. Verify Python deps install, TS deps install, config file is created, legacy `.env` is migrated, legacy systemd units are cleaned up, user is left in a working state.

#### Phase 6 — Delete the obsolete plumbing

Once Phases 1–5 are working, delete:

- `scripts/voice-stt-svc` (entire file — 160+ lines)
- `scripts/claude-voice` (entire file — or shrink to a 3-line launcher symlinked by `/voice-stt:install`)
- `scripts/install-systemd-units` (entire file)
- `scripts/systemd/voice-sttd.service.in`
- `scripts/systemd/voice-stt-ptt.service.in`
- `scripts/systemd/` directory
- `.env.example` at repo root (superseded by `plugin/config.template`)
- `.env` at repo root (user's machine-specific config, not tracked anyway — but the install flow removes the repo dependency so `.env` shouldn't live there)
- `src/voice_stt/ptt_listener.py` (folded into daemon as thread in Phase 2)
- `src/voice_stt/client.py` (nothing in the new architecture calls it — the ctrl socket is gone and `listen` has no out socket to read from). Consider keeping a minimal debug `listen` that reads from a `--debug-log` file if we decide we want it.
- Entries from `pyproject.toml::[project.scripts]`: drop `voice-stt` (client CLI) and `voice-stt-ptt` (standalone listener). Keep `voice-sttd`.
- `plugin/.mcp.json` may not need to change — it already points at `${CLAUDE_PLUGIN_ROOT}/channel/voice-stt-channel.ts`.

#### Phase 7 — Docs

- **README.md:** rewrite the `One-time setup` and `Run` sections around `/plugin marketplace add` → `/plugin install` → `/voice-stt:install`. Delete the systemd section entirely. Delete the "Advanced: manual three-terminal workflow" — it no longer applies. Keep the "Audio routing: alternating silent buffers" troubleshooting section (now refers to editing `~/.config/voice-stt/config` instead of `.env` + `systemctl restart`).
- **`docs/plans/2026-04-11-feat-voice-stt-claude-code-channel-plan.md`:** add a postmortem line: "followed in 2026-04-12 refactor plan — the daemon architecture was collapsed into the plugin, deleting the systemd and svc-wrapper layers. Multi-session VRAM sharing was never needed."
- **Obsidian vault `voice-stt-claude-code-setup.md`:** update the architecture diagram and daily workflow. New launch: `claude --dangerously-load-development-channels plugin:voice-stt@voice-stt-local` (plus whatever alias the user sets up).
- **Obsidian vault `razer-naga-button-remapping.md`:** no changes needed — the PTT-key → evdev pattern is unchanged, it just runs inside the daemon process now instead of as a separate service.

## Alternatives considered

1. **Keep the daemon + systemd architecture, just add `/voice-stt:install` for the existing setup.**
   Less invasive. Matches voicemode's ergonomics only for the install flow; still leaves systemd management as the lifecycle story. Doesn't delete any of the complexity the review agents flagged. **Rejected** — the install-ergonomics win is nice but the architectural cleanup is the bigger prize, and they're cheap to do together.

2. **Rewrite the channel MCP server in Python, drop Bun/TS entirely.**
   Simpler: single language, single process, no subprocess orchestration. The Python MCP SDK supports stdio transport, so the channel server could host Whisper directly. But: the Claude Code channels API is documented with TypeScript SDK examples (https://code.claude.com/docs/en/channels-reference), and the `claude/channel` experimental capability hasn't been demonstrated on the Python MCP SDK in public docs. Adopting an undocumented path is risky for a research-preview feature. **Rejected for now** — revisit once Python MCP SDK support is confirmed, and only if the Bun-spawns-Python approach turns out to be more painful than expected.

3. **Use a unix socket between the TS channel and the Python daemon (instead of stdin/stdout).**
   More flexible: multiple TS channels could connect to one Python daemon. But we just established multi-session is a phantom requirement. Unix sockets also reintroduce the bind→chmod race, permissions, path selection (we just moved off `/tmp`), and cleanup. **Rejected** — stdin/stdout is the correct idiom for parent/child IPC.

4. **Spawn the Python daemon once at plugin install time and leave it running forever.**
   This is systemd again, reinvented inside the plugin install flow. **Rejected** trivially.

5. **Keep both architectures — systemd for "power users," collapsed plugin for new users.**
   Complexity explosion. Two install paths, two test matrices, two bugfix surfaces. **Rejected** — pick the simpler one and commit to it.

6. **Collapse the daemon into the TS process itself** (call into a Whisper WASM build, or use `ctranslate2-js` or similar).
   Interesting but Whisper on WASM is slow, GPU acceleration is Python-only, and the existing faster-whisper stack is the right choice for a RTX-class GPU. **Rejected** — the subprocess spawn isn't the expensive part.

## System-wide impact

### Interaction graph

```
Claude Code launches the channel plugin (MCP server)
   └─ Bun process (voice-stt-channel.ts)
        ├─ mcp.connect(StdioServerTransport) — handshake with Claude Code
        ├─ Bun.spawn(['uv', 'run', 'voice-sttd'])
        │    └─ Python child
        │         ├─ singleton.acquire_or_exit() — fcntl.flock on daemon.lock, exits code 2 if held
        │         ├─ config.load() — reads ~/.config/voice-stt/config
        │         ├─ _bridge_pulse_source() — translates VOICE_STT_PULSE_SOURCE → PULSE_SOURCE
        │         ├─ Daemon.__init__
        │         │    ├─ faster_whisper.WhisperModel(...)   # ~2s on medium.en
        │         │    ├─ sd.InputStream(device=..., callback=_audio_cb).start()
        │         │    ├─ threading.Thread(target=_transcribe_worker).start()
        │         │    └─ threading.Thread(target=_ptt_listener_loop).start()
        │         └─ main loop: read stdin lines, drive start_recording/stop_recording
        │
        └─ for await chunk of child.stdout:
             parse lines → mcp.notification('notifications/claude/channel', {content, meta})
               └─ Claude Code session receives <channel source="voice-stt" ...> event
                    └─ Claude reads the event as user input and responds
```

PTT key press in-session: `evdev_thread.reads(event) → calls daemon.start_recording() directly (method call in same process) → sets _capturing=True → audio_cb starts buffering frames → keypress release → daemon.stop_recording() → queue audio to transcribe_worker → worker.transcribe() → writes line to stdout → Bun reader → mcp.notification → Claude sees the message`.

### Error & failure propagation

| Failure | Where it surfaces | Recovery |
|---|---|---|
| Python child fails to load Whisper model (CUDA OOM) | Python stderr → Bun forwards to TS stderr → Claude Code's debug log | TS channel restarts child up to 3× in 30s, then gives up with a persistent error message |
| Python child segfaults / exits unexpectedly | `child.exited` fires in TS; stdout reader hits EOF | Same restart policy |
| Python child hangs forever (never prints anything) | TS channel has no way to know; keyboard PTT press produces no output | User notices, they can check Claude Code's debug log. Mitigation: add a `ready\n` line the daemon prints after `_transcribe_worker` is started, and have TS log if that doesn't arrive within 10 seconds. |
| TS channel dies mid-session | Python child gets stdin EOF (stdout pipe also closes); stdin reader thread calls `sys.exit(0)` cleanly | Python dies with it. Next session starts fresh. |
| Mic device gone (USB unplug) | sd.InputStream raises, daemon logs to stderr | Rescan + reattach happens in the existing persistent-stream code (no change from current behavior). |
| evdev device gone (input-remapper reload) | PTT thread sees OSError, drops fd, rescans | Inherited from the review-fixes branch (`5a3f391`). |
| Config file has a malformed line | `config.load()` silently skips the bad line | No crash. Value falls back to default. |
| Config file has unexpected key | `config.load()` exports it anyway (we only validate key name is a valid identifier); daemon ignores unknown env vars | Harmless. |
| User has no XDG_CONFIG_HOME set | `config.load()` falls back to `$HOME/.config` | Unchanged from XDG spec behavior. |
| User runs a second Claude Code session while first is active | Second Python child calls `singleton.acquire_or_exit()`, fcntl.flock fails with EWOULDBLOCK, child writes the diagnostic message to stderr and exits code 2 | TS channel sees `exited == 2` (our sentinel for "permanent lock contention"), bypasses the retry loop, logs the persistent error immediately. User sees "another voice-stt daemon is already running" in Claude Code's debug log. See the [Single-session enforcement](#single-session-enforcement-first-class-design-element) subsection for full design. |

### State lifecycle risks

- **Whisper model loaded in VRAM, dies with session.** Clean — no orphaned GPU memory. Model cache on disk (`~/.cache/huggingface/hub/...`) is preserved across sessions, so subsequent loads are fast (disk → VRAM ~1s, vs. the first-run download ~30s for medium.en).
- **sd.InputStream open, dies with process.** PortAudio releases the device handle on Python exit, clean.
- **Transcription queue state when the child is killed.** Any audio enqueued but not yet transcribed is lost. Acceptable — user can just repeat the utterance. Queue is in-process, not on disk, so no orphan state.
- **PTT thread holding evdev fds.** Released on process exit.
- **Stale lockfile after unclean exit.** Use `fcntl.flock(..., LOCK_EX | LOCK_NB)` — advisory lock is automatically released by the kernel when the holding process dies, even on SIGKILL. No stale state.
- **First-install migration from legacy .env.** Idempotent by design: `/voice-stt:install` detects the old file, copies content, leaves the old file in place (doesn't delete — user can verify the migration before removing it themselves).
- **Legacy systemd units during migration.** The install command optionally stops and removes them. If the user refuses the cleanup, the old systemd services stay — potentially confusing (the units will fail to start because the unit files reference paths that no longer exist), but not harmful.

### API surface parity

| Interface | Before | After |
|---|---|---|
| User PTT key | `voice-stt-ptt` service reads evdev, sends `start`/`stop` over ctrl socket | PTT thread inside daemon calls `start_recording()` / `stop_recording()` directly |
| Consumers of transcripts | Unix out socket, fan-out to N clients (listen, type, clip, channel) | Stdout pipe to the single TS parent (channel) |
| Daemon control | `voice-stt start`/`stop` over ctrl socket | stdin lines to the child, or direct method calls from PTT thread |
| Config | `.env` in repo, parsed by bash + systemd EnvironmentFile | `$XDG_CONFIG_HOME/voice-stt/config`, parsed by Python |
| Lifecycle | `voice-stt-svc start` / systemctl user units | Claude Code session start/exit |
| Health check | `systemctl --user is-active voice-sttd.service` | `child.exited` flag in TS |
| Launch | `claude-voice` wrapper | Raw `claude --dangerously-load-development-channels plugin:voice-stt@voice-stt-local` (or a shell alias / symlink created by `/voice-stt:install`) |

The CLI surface for users gets **strictly smaller**: where before there were 3 client subcommands (start/stop/listen), one wrapper (claude-voice), one svc supervisor, and 4 systemctl verbs, after there is one slash command (`/voice-stt:install`) plus the raw `claude` launch command. No one manages processes by hand.

### Integration test scenarios

These can't be unit-tested — they require a real mic, real hardware, real GPU, real Claude Code. Manual smoke tests:

1. **Fresh install on a clean system.**
   Clone the repo (or `/plugin marketplace add` in Claude Code), run `/voice-stt:install`, verify:
    - Python deps install, TS deps install, config file created from template
    - Launch `claude --dangerously-load-development-channels plugin:voice-stt@voice-stt-local`
    - Press PTT key, speak, verify one `<channel source="voice-stt">` event arrives
    - Verify only one new Python process is running (`pgrep -f voice-sttd`), parent is the bun process
    - `/exit` Claude Code, verify the Python child dies within 2 seconds

2. **Migration from the current systemd setup.**
   On a machine that already has `voice-sttd.service` running and `~/projects/voice-stt/.env` configured:
    - Pull the refactored branch, `/voice-stt:install`
    - Verify the install command detects the existing systemd units and offers to disable them
    - Verify `.env` contents are copied to `~/.config/voice-stt/config`
    - Restart Claude Code with the new flag
    - Verify transcription still works with the user's PULSE_SOURCE override preserved

3. **Python child crash recovery.**
   Launch with the channel, dictate one phrase successfully. `pkill -f voice-sttd` while in the session. Dictate again, verify:
    - TS channel notices the child exit, logs an error to stderr
    - TS spawns a new Python child (with a fresh 2-3s model reload)
    - PTT press on the new child produces a transcript
    - Restart counter increments; after 3 consecutive crashes in 30s, stops restarting and logs a persistent error

4. **Parent exit cleanup.**
   Launch with the channel, find the Python pid, `kill -9 <bun_pid>` (simulate TS crash). Verify the Python child dies on its own within 1 second (stdin EOF path).

5. **Concurrent sessions.**
   With one Claude Code session already running the channel, launch a second one. Verify:
    - Second session's Python child acquires the lockfile, sees it held, exits with clear error
    - Second session's TS channel logs the error and doesn't restart
    - First session is unaffected
    - `/exit` first session, launch second one: lock is released, second session works fine

## Acceptance criteria

### Functional

- [ ] **Fresh install flow works in 3 Claude Code commands.** `/plugin marketplace add MaxInertia/claude-voice-input-channel` + `/plugin install voice-stt@voice-stt-local` + `/voice-stt:install`, then a normal Claude Code launch with the channel flag, yields a working dictation pipeline with no other user action (other than mapping a hardware button to the PTT key via input-remapper, which is unavoidable).
- [ ] **Dictation produces one channel event per PTT press**, same as today. Segment join from commit `b7cb1d8` is preserved.
- [ ] **Python child lifecycle is tied to Claude Code session.** Exiting Claude Code causes the Python child to exit within 2 seconds. Killing the TS process causes the same.
- [ ] **Config file at `~/.config/voice-stt/config`** is read on daemon startup. `VOICE_STT_PULSE_SOURCE` override works end-to-end (verified by the alternating-silence bug NOT recurring on the dev machine).
- [ ] **Precedence chain preserved.** CLI arg > shell env > config file > builtin default, as verified by setting `VOICE_STT_MODEL=small.en` in the config file, shell env `VOICE_STT_MODEL=large-v3`, and confirming the shell env wins.
- [ ] **Migration path from legacy systemd setup works** on the dev machine without data loss. Old `.env` content appears in the new config file. Old systemd units are stopped + removed.
- [ ] **Python child crash is recoverable** — TS restarts it with a small backoff, within 3 retries in 30 seconds.
- [ ] **Concurrent session guard** — a second Claude Code session with the channel plugin detects the lockfile and refuses to start a second daemon. Verified by: launch Claude Code A with the channel, confirm daemon.lock is held, launch Claude Code B with the channel, confirm B's Python child exits with code 2 and B's Claude Code debug log contains the "another voice-stt daemon is already running" message. Exit Claude Code A, launch a new Claude Code C, confirm C's daemon starts cleanly (lock was released on A's exit).

### Non-functional

- [ ] **LOC reduction is net negative by at least 300 lines.** Delete ~500 of bash/systemd/wrapper code, add ~150 of TS subprocess orchestration + Python config loader + slash command markdown.
- [ ] **Channel plugin startup ≤ 5s** from `claude` launch to "channel connected" (Whisper load ~2-3s + Bun spawn overhead + MCP handshake).
- [ ] **First PTT press ≤ 1s latency** from press to "recording: ON" log line, after the initial warmup.
- [ ] **No systemd dependency** in any code path.
- [ ] **No unix sockets** in any code path.
- [ ] **No `/tmp` filesystem usage** for IPC (lockfile lives under `$XDG_RUNTIME_DIR/voice-stt/`).
- [ ] **Python daemon is standalone-runnable** for debugging: `uv run voice-sttd` in a terminal still works, drives via stdin, prints to stdout.

### Quality gates

- [ ] `uv run voice-sttd < /dev/null` exits cleanly when stdin closes (no hanging, no stack trace).
- [ ] `pgrep -f voice-sttd` returns exactly one process after `/voice-stt:install` + launching Claude Code + no PTT presses.
- [ ] `pgrep -f voice-sttd` returns zero processes 2 seconds after `/exit` from Claude Code.
- [ ] README walks a reader from "git URL" to "speaking to Claude" in fewer steps than the legacy instructions did. Specifically: no mention of `clone`, `systemctl`, `uv sync`, `bun install`, `apt install` in the primary path (all of those get hidden inside `/voice-stt:install`).
- [ ] No references to `voice-stt-svc`, `claude-voice`, `install-systemd-units`, `voice-sttd.service`, or `voice-stt-ptt.service` in the main README or in the obsidian vault's voice-stt setup doc. Historical postmortem mentions in the plan doc are fine.

## Dependencies & prerequisites

- **Existing:** `uv`, `bun`, `libportaudio2`, NVIDIA driver for GPU path.
- **No new runtime dependencies.** `faster-whisper`, `sounddevice`, `numpy`, `evdev`, `nvidia-cublas-cu12`, `nvidia-cudnn-cu12` all stay. `@modelcontextprotocol/sdk` stays.
- **Claude Code version:** requires whatever version supports custom channel plugins via `/plugin install` and `--dangerously-load-development-channels plugin:...` — currently in research preview, working on `2.1.104` on the dev machine.
- **No systemd requirement** — the plan removes this dependency from the critical path.
- **No repo clone requirement for end users** — the plugin marketplace flow handles it. Contributors/hackers will still clone, obviously.

## Risk analysis & mitigation

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Bun.spawn parent-child stdio handling has edge cases on some Linux kernels | Medium | Low | Use `node:child_process.spawn` as a fallback if Bun's version misbehaves. Both have been stable on Linux for years. |
| Python child hangs on stdin read when parent dies, because Python's default stdin buffering doesn't respect EOF semantics cleanly | Medium | Medium | Use `sys.stdin.buffer.readline()` in a daemon thread with `bufsize=0` on spawn, OR poll `os.getppid() == 1` every second and exit if the parent became init (orphan detection). I'll go with the stdin-EOF approach first and add the ppid poll as belt-and-braces. |
| Slash commands don't have access to all the shell commands the install step needs, because the agent's Bash tool might be sandboxed | Medium | Low | The agent's bash already runs `uv`, `bun`, `cp`, `mkdir`, `systemctl`, `apt list`, `chmod` — all the operations we need. Same sandboxing applies to every other Claude Code install script, so this is well-trodden ground. |
| Plugin cache directory gets nuked on plugin update, taking the `uv sync` venv with it | Low | Certain | Acceptable — `uv sync` re-creates the venv in ~10 seconds using its global wheel cache at `~/.cache/uv`. No download on the second install. |
| `/voice-stt:install` is re-run and overwrites the user's customized config file | High | Low | Install command explicitly detects an existing config and leaves it alone. Only writes the template if the file is absent. |
| Python MCP SDK channels work on the Bun side but a future Python rewrite hits undocumented semantics | Low (for this plan) | N/A | Not in scope for this refactor. |
| The PTT thread inside the daemon is more fragile than a separate process | Low | Low | evdev reads happen in their own thread; any OSError is caught and handled by the rescan loop. Same code as the existing `ptt_listener.py` (`5a3f391`), just moved into a thread. |
| Two concurrent Claude Code sessions — lock contention is ugly UX | Medium | Medium | Clear error message: "another voice-stt session is active (holding $XDG_RUNTIME_DIR/voice-stt/daemon.lock). Exit the other Claude Code session first." Don't try to be clever about sharing. |
| Migration from legacy setup partially completes (e.g., systemd disable works but `.env` copy fails) | Medium | Low | Each migration step logs its outcome and the install command continues with what it can. User can re-run to finish. No destructive steps unless asked. |
| CUDA out-of-memory at startup (background GPU users) | Medium | Medium | Same as today: daemon logs the error to stderr, TS forwards to Claude Code debug log, user sees it. Document the fix in README ("close gpu-screen-recorder or switch to `VOICE_STT_MODEL=small.en`"). |

## Migration plan

For the dev machine (the only current install):

1. **Pre-flight:** `systemctl --user status voice-sttd.service voice-stt-ptt.service` (should be running). Note any custom PULSE_SOURCE in `~/projects/voice-stt/.env`.
2. **Checkout the refactor branch.** Pull and rebuild locally.
3. **Refresh the plugin cache** (as we did for every other branch change today): `/plugin marketplace update voice-stt-local` + `/plugin uninstall voice-stt@voice-stt-local` + `/plugin install voice-stt@voice-stt-local` + `/reload-plugins`.
4. **Run `/voice-stt:install`** inside the session. Let it:
   - `uv sync` in the cached plugin dir (fast — wheels already cached)
   - `bun install` in the cached plugin dir (fast — lockfile hits)
   - Detect old `.env`, offer to copy to `~/.config/voice-stt/config`
   - Detect old systemd units, offer to disable and remove
5. **Exit Claude Code.** Relaunch with `claude --dangerously-load-development-channels plugin:voice-stt@voice-stt-local`.
6. **Test dictation** — press the PTT key, verify one channel event arrives.
7. **Verify cleanup:** `systemctl --user status voice-sttd.service` should show "not found" after the migration cleanup.

If anything goes wrong, rollback is: `git checkout master` (the `review-fixes` branch merge state), re-run the old `scripts/install-systemd-units`, `systemctl --user enable --now voice-sttd.service voice-stt-ptt.service`.

## Documentation plan

- **README.md** — Rewrite `One-time setup` and `Run` sections. Delete the "Legacy: voice-stt-svc wrapper" subsection. Delete the "Manual: three-terminal workflow" Advanced subsection. Keep the "Audio routing: alternating silent buffers" troubleshooting subsection but update paths (`~/.config/voice-stt/config` instead of `.env` + `voice-stt-svc restart`).
- **`docs/plans/2026-04-11-feat-voice-stt-claude-code-channel-plan.md`** — Add one line to the Risks section noting that the subsequent refactor (this plan) collapsed the daemon into the plugin, and the multi-session VRAM argument that justified the daemon was retrospectively wrong.
- **`~/obsidian-vault/docs/voice-stt-claude-code-setup.md`** — Update the architecture diagram, daily workflow, and one-time setup checklist. The Naga-specific button wiring section in `razer-naga-button-remapping.md` is unchanged — input-remapper → KEY_F20 → PTT listener still applies, the listener just runs inside the daemon process now.
- **No changes needed to** the channels reference docs or the Razer Naga doc's preset JSON.

## Sources & references

### Internal

- [docs/plans/2026-04-11-feat-voice-stt-claude-code-channel-plan.md](./2026-04-11-feat-voice-stt-claude-code-channel-plan.md) — the prior plan that established the three-process architecture we're now collapsing.
- PR #1 (`review-fixes` branch, commits `5a3f391` through `f9f6905`, merged into master as of 2026-04-12) — adds systemd units. This plan reverses the systemd path while keeping all the review-fix bug fixes (PTT rescan, transcription worker, broadcast lock, etc.).
- `src/voice_stt/daemon.py::_bridge_pulse_source` (added in `f9f6905`) — the env-var bridge that lets `systemd EnvironmentFile=` pass `VOICE_STT_PULSE_SOURCE` to the daemon. Stays useful in the new world because `~/.config/voice-stt/config` is also parsed into env vars and needs the same bridge.
- `src/voice_stt/ptt_listener.py::_find_keyboards` and the rescan loop (`5a3f391`) — moves into the PTT thread inside the daemon process in Phase 2. Code logic is unchanged.
- `plugin/channel/voice-stt-channel.ts` — current ~90-line channel server, rewritten in Phase 4 to own a Python subprocess.

### External

- [voicemode project (https://github.com/mbailey/voicemode)](https://github.com/mbailey/voicemode) — the project whose install ergonomics we're targeting. Their 4-command install was the concrete prompt for this refactor.
- [Claude Code channels reference](https://code.claude.com/docs/en/channels-reference) — channel capability protocol.
- [Claude Code plugins docs](https://code.claude.com/docs/en/plugins) — slash command format (`plugin/commands/<name>.md` markdown files).
- [Claude Code plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces) — `/plugin marketplace add owner/repo` GitHub shorthand syntax, which is what makes the 3-command install possible.
- [Bun.spawn docs](https://bun.sh/docs/api/spawn) — subprocess management API for the TS channel server's child-process handling.
