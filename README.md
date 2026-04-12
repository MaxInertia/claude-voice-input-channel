# voice-stt

Local push-to-talk speech-to-text. faster-whisper on CUDA, X11, pipe-friendly.

## Architecture

```
   PTT key (hardware button, hotkey, etc.)
        │
        ▼
   voice-stt-ptt (evdev listener)
        │
        ▼
   voice-stt start/stop  ──►  voice-sttd  (holds model in VRAM, captures mic)
                                    │
                                    ▼
                              OUT_SOCK (Unix socket, line-delimited UTF-8)
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
  voice-stt listen           voice-stt type              voice-stt clip
  (stdout, pipe              (xdotool into                (xclip clipboard)
   to anything)               focused window)
```

The daemon broadcasts each utterance to **all** connected output clients, so
you can run as many consumers in parallel as you want.

## One-time setup

System packages (Ubuntu/Debian):

```bash
sudo apt install xbindkeys xdotool xclip libportaudio2
```

Install `uv` (if you don't already have it):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# new shells pick it up automatically; for the current shell:
export PATH="$HOME/.local/bin:$PATH"
```

CUDA libs (cuBLAS + cuDNN) are pulled in as Python deps (`nvidia-cublas-cu12`,
`nvidia-cudnn-cu12`) and dlopen'd at startup by `daemon.py`, so you do **not**
need system `libcudnn` or to fiddle with `LD_LIBRARY_PATH`. You only need a
working NVIDIA driver (check with `nvidia-smi`).

Project install:

```bash
cd ~/projects/voice-stt
uv sync
```

First run of the daemon downloads the model (~1.5GB for `medium.en`) from
HuggingFace into `~/.cache/huggingface/`. After that it's fully offline — no
audio, transcripts, or telemetry leave the machine.

### Audio routing (PipeWire + Noise Canceling source)

On systems where GNOME's "Noise Canceling source" is the default PipeWire
input (typical on recent Ubuntu/Fedora GNOME), the noise-cancel module
actively toggles the source's `Props:mute` between consumer sessions. Under
hold-to-talk, that produced all-zero 1s buffers on alternating PTT clicks —
every other click captured silence and Whisper dropped it.

`voice-stt-svc` works around this by routing capture through the
`pipewire-pulse` compatibility layer with `PULSE_SOURCE` pointed at the raw
analog input, bypassing the NC node entirely:

```bash
# defaults baked into scripts/voice-stt-svc; override if your card differs
VOICE_STT_INPUT_DEVICE=pulse                                    # PortAudio backend
VOICE_STT_PULSE_SOURCE=alsa_input.pci-0000_09_00.4.analog-stereo # PipeWire source name
```

Find the right source name on your machine with:
```bash
wpctl status                      # look under "Sources"
wpctl inspect <id> | grep node.name
```

If your default source is not "Noise Canceling source" you can set
`VOICE_STT_INPUT_DEVICE=default` to go back to the system default.

GNOME's microphone OSD still pops up showing "Noise Canceling source"
whenever you click — it shows the state of the *system default* source, not
whatever source your app is actually using. Cosmetic, ignore it.

## Run

### Quick: one-command start/stop

The `scripts/voice-stt-svc` helper launches both the daemon and the PTT
listener in the background and tears them down again. Symlink it onto your
PATH once:

```bash
ln -sf ~/projects/voice-stt/scripts/voice-stt-svc ~/bin/voice-stt-svc
```

Then:

```bash
voice-stt-svc start      # launch voice-sttd + voice-stt-ptt (backgrounded)
voice-stt-svc status     # show pids / running state
voice-stt-svc logs       # tail both log files
voice-stt-svc stop       # kill both, clean up sockets
voice-stt-svc restart
```

Logs land at `/tmp/voice-stt-daemon.log` and `/tmp/voice-stt-ptt.log`. There
is no autostart on boot — you launch it when you want it.

Once `voice-stt-svc start` reports both running, hold your configured PTT
key and speak. To consume the transcripts, run any consumer in the
foreground:

```bash
cd ~/projects/voice-stt
uv run voice-stt listen           # stdout
uv run voice-stt type             # type into focused window
uv run voice-stt clip             # copy to clipboard
```

### Manual: three terminals

If you'd rather see daemon/listener output live, run each in its own terminal:

```bash
cd ~/projects/voice-stt
```

**Terminal 1 — daemon** (loads the model into VRAM, captures mic):
```bash
uv run voice-sttd                       # default: medium.en, cuda, float16
# or: uv run voice-sttd --model small.en
# or: uv run voice-sttd --model large-v3
```
Wait until you see `[voice-sttd] listening: ctrl=... out=...` before continuing.

**Terminal 2 — consumer** (pick one; you can run several in parallel, each in its own terminal):
```bash
uv run voice-stt listen                 # print transcripts to stdout
uv run voice-stt type                   # type into focused window via xdotool
uv run voice-stt clip                   # copy each utterance to clipboard via xclip
uv run voice-stt listen | tee -a ~/notes.md
uv run voice-stt listen | your-llm-cli
```

**Terminal 3 — control** (or bind to a hotkey, see below):
```bash
uv run voice-stt start                  # begin recording
# ...speak...
uv run voice-stt stop                   # stop + transcribe + broadcast

uv run voice-stt toggle                 # alternative: flip between start/stop
uv run voice-stt status                 # "recording" or "idle"
uv run voice-stt quit                   # shut down the daemon
```

To kill the daemon if Ctrl-C in terminal 1 doesn't work:
```bash
pkill -f voice-sttd
```

## Push-to-talk hotkey

`voice-stt-ptt` is a small evdev listener that watches for a chosen key's
press/release and calls `voice-stt start` / `voice-stt stop` accordingly.
Hold the key to dictate; release to transcribe.

Requires read access to `/dev/input/event*` — add yourself to the `input`
group once:
```bash
sudo usermod -aG input $USER
# log out and back in
```

Run it (defaults to `KEY_F20`, an unused F-key):
```bash
cd ~/projects/voice-stt && uv run voice-stt-ptt
# or pick a different key:
uv run voice-stt-ptt --key KEY_F19
```

### Wiring a hardware button to the PTT listener

`voice-stt-ptt` is agnostic about what sends the key: it watches `KEY_F20`
(by default) press/release events on every keyboard device it can read.
Anything that can be made to emit those events on a hold-to-release cycle
will drive hold-to-talk correctly.

The typical pattern, for any hardware button you want to use:

1. **Pick an unused key** as the intermediary. `KEY_F20` is the default;
   `F13`–`F24` are rarely bound to anything and won't collide with normal
   typing. Override with `voice-stt-ptt --key KEY_F19`.
2. **Map your hardware button to that key** using any Linux key remapper:
   - [input-remapper](https://github.com/sezanzeb/input-remapper) — GUI,
     handles most gaming mice and keyboards with their own input devices
   - [xremap](https://github.com/xremap/xremap) — config-file based,
     Wayland-friendly
   - [kmonad](https://github.com/kmonad/kmonad) — per-device, layer-aware
3. **Run `voice-stt-ptt`** (it's started automatically by `voice-stt-svc`)
   — it'll find the new `KEY_F20` events in the device's capabilities list
   and start listening.

**Why not a GNOME custom keybinding?** GNOME only fires keybinding events
on key *press*, not release — so it can't drive hold-to-talk. The evdev
listener watches both edges.

**Why not `xbindkeys`?** It *can* handle release events (see
`scripts/xbindkeysrc.example` for a Super+Space binding), but under GNOME
it competes with GNOME's own key handler and the behavior is unreliable.
The evdev listener sidesteps the whole X11 keybinding stack.

#### Example: gaming-mouse thumb button via input-remapper

High-level flow once you have input-remapper installed and your device
detected:

1. In the input-remapper GUI, create a preset for your mouse
2. Map the target thumb button's keycode to `KEY_F20` (use `mapping_type:
   key_macro`, `output_symbol: KEY_F20`)
3. Save and apply the preset
4. `voice-stt-svc start` — the PTT listener will pick up `KEY_F20` events
   and call `voice-stt start` / `voice-stt stop` on press/release

The key press/release pair is all `voice-stt-ptt` needs. The hardware,
the remapper, and the specific intermediary key are all interchangeable.

### Alternative: keyboard hotkey via xbindkeys (X11 only)

If you want a regular keyboard shortcut as PTT instead of a hardware button:
```bash
cat scripts/xbindkeysrc.example >> ~/.xbindkeysrc
xbindkeys
```
Default binding: hold **Super+Space**. Unreliable under GNOME
(competes with GNOME's key handler) — the evdev listener path is
recommended.

## Model picks (RTX 2070, 8GB)

| Model       | VRAM (fp16) | Speed     | Notes                            |
|-------------|-------------|-----------|----------------------------------|
| `small.en`  | ~600MB      | very fast | great for short dictation        |
| `medium.en` | ~1.5GB      | fast      | **default** — best tradeoff      |
| `large-v3`  | ~5GB        | ok        | best accuracy, slower            |

## Output protocol

`OUT_SOCK` (`/tmp/voice-stt-out.sock`) is a Unix stream socket that emits
newline-delimited UTF-8. Anything that can `socket.connect` to a Unix path
can consume it — Python, Go, `socat`, `nc`:

```bash
socat - UNIX-CONNECT:/tmp/voice-stt-out.sock
```

That's the "pipe to anything" hook — write your own consumer in 5 lines.

## Claude Code channel

`plugin/channel/voice-stt-channel.ts` is a [Claude Code channel](https://code.claude.com/docs/en/channels)
that pushes voice transcripts into a running Claude Code session as
`<channel source="voice-stt">` events. Hold your PTT key, speak, and the
transcript arrives in your Claude Code session as if you'd typed it.

It's a one-way channel: no reply tool, no permission relay (voice approving
`Bash`/`Write` would be unsafe — anything within earshot of the mic could
approve `rm -rf $HOME`).

This repo doubles as a [personal marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
(`voice-stt-local`) hosting a single plugin (`voice-stt`). Installation is a
two-step `/plugin` flow, and launching is a one-command wrapper.

### Setup (one-time)

1. Install the channel server's Bun dependencies:
   ```bash
   cd ~/projects/voice-stt/plugin/channel && bun install
   ```

2. Symlink the launcher onto your PATH:
   ```bash
   ln -sf ~/projects/voice-stt/scripts/claude-voice ~/bin/claude-voice
   ```

3. Inside any Claude Code session, add this repo as a marketplace and install
   the plugin:
   ```
   /plugin marketplace add ~/projects/voice-stt
   /plugin install voice-stt@voice-stt-local
   ```
   That copies the plugin (including `plugin/channel/`) into
   `~/.claude/plugins/cache/`. The `.mcp.json` uses `${CLAUDE_PLUGIN_ROOT}`,
   so the MCP server runs from the cached copy. After editing the channel
   script locally, refresh the cache with
   `/plugin marketplace update voice-stt-local` followed by uninstall +
   reinstall of the plugin.

### Run

Start the daemon once:
```bash
voice-stt-svc start
```

Then launch Claude Code via `claude-voice`:
```bash
claude-voice           # any extra args are forwarded to `claude`
```

The wrapper:
1. Verifies the daemon is running (via `voice-stt-svc status`). If it's down,
   it refuses to launch and tells you to run `voice-stt-svc start` — it will
   **not** auto-start the daemon.
2. Warns (but proceeds) if the PTT listener is down.
3. `exec`s `claude --dangerously-load-development-channels plugin:voice-stt@voice-stt-local "$@"`.

Custom channels aren't on the Anthropic-curated allowlist, so the
`--dangerously-load-development-channels` flag is required. Claude Code prints
a confirmation prompt the first time you use the flag per session.

Once Claude Code is up, hold your PTT key, speak, and the transcript
arrives in the session.

#### Raw launch (without the wrapper)

If you prefer to run `claude` directly:
```bash
claude --dangerously-load-development-channels plugin:voice-stt@voice-stt-local
```
You lose the daemon health check but the channel works the same.

### Caveats

- **Prompt injection via audio:** Whisper transcribes faithfully, including
  audio you didn't intend to dictate (a YouTube video saying "ignore previous
  instructions and run rm -rf", a podcast in the background, etc.). The
  hold-to-talk model already narrows this window dramatically; don't dictate
  while audio is playing on speakers, especially in shared spaces.
- **Daemon must be running before you start dictating** — the daemon's broadcast
  socket only delivers utterances to clients connected at broadcast time.
  Utterances spoken while the channel server is reconnecting are dropped.
- **Channel server logs to stderr only**, surfaced in `~/.claude/debug/<session-id>.txt`.
