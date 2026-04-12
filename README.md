# voice-stt

Local push-to-talk speech-to-text. faster-whisper on CUDA, X11, pipe-friendly.

## Architecture

```
   PTT key (Naga `=`, hotkey, etc.)
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

Once `voice-stt-svc start` reports both running, hold the Razer Naga `=`
button (or whatever PTT key you've configured) and speak. To consume the
transcripts, run any consumer in the foreground:

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

### Razer Naga `=` button (current setup)

The Razer Naga's thumb-grid `=` button is Linux keycode 13 (`KEY_EQUAL`).
GNOME custom keybindings can't do hold-to-talk (they only fire on press),
so we route around them:

1. **input-remapper** rewrites Naga `=` (code 13) → `KEY_F20`
2. **voice-stt-ptt** listens for `KEY_F20` press/release across all keyboards
3. On press → `start`, on release → `stop`

The input-remapper preset is at:
```
~/.config/input-remapper-2/presets/Razer Razer Naga V2 HyperSpeed/Replay Save.json
```

After editing the preset, reload it:
```bash
input-remapper-control --command stop  --device "Razer Razer Naga V2 HyperSpeed"
input-remapper-control --command start --device "Razer Razer Naga V2 HyperSpeed" --preset "Replay Save"
```

See `~/obsidian-vault/docs/razer-naga-button-remapping.md` for the broader
Naga remapping setup.

### Alternative: keyboard hotkey via xbindkeys

If you want a regular keyboard PTT hotkey instead (X11 only):
```bash
cat scripts/xbindkeysrc.example >> ~/.xbindkeysrc
xbindkeys
```
Default binding: hold **Super+Space**. Note that on GNOME, xbindkeys may
conflict with GNOME's own keybinding handler — `voice-stt-ptt` is more
reliable.

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

`channel/voice-stt-channel.ts` is a [Claude Code channel](https://code.claude.com/docs/en/channels)
that pushes voice transcripts into a running Claude Code session as
`<channel source="voice-stt">` events. Hold the Naga `=` button, speak, and
the transcript arrives in your Claude Code session as if you'd typed it.

It's a one-way channel: no reply tool, no permission relay (voice approving
`Bash`/`Write` would be unsafe — anything within earshot of the mic could
approve `rm -rf $HOME`).

### Setup (one-time)

1. Install the Bun dependencies:
   ```bash
   cd ~/projects/voice-stt/channel && bun install
   ```

2. Register the MCP server in `~/.claude.json`:
   ```bash
   jq '.mcpServers["voice-stt"] = {
     "command": "/home/maxinertia/.bun/bin/bun",
     "args": ["/home/maxinertia/projects/voice-stt/channel/voice-stt-channel.ts"]
   }' ~/.claude.json > ~/.claude.json.new && mv ~/.claude.json.new ~/.claude.json
   ```

### Run

Make sure the daemon is up first:
```bash
voice-stt-svc start
```

Then launch Claude Code with the channel enabled:
```bash
claude --dangerously-load-development-channels server:voice-stt
```

Custom channels aren't on the Anthropic-curated allowlist, so the
`--dangerously-load-development-channels` flag is required. It prints a
confirmation prompt on first use per session.

Once Claude Code is up, hold the Naga `=` button (or whatever PTT key you've
configured), speak, and the transcript arrives in the session.

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
