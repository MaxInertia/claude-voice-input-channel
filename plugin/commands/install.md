---
description: Install voice-stt system dependencies and create the user config file
---

You are setting up voice-stt for the user on this machine. Work through
these steps in order, run the shell commands via your Bash tool, and
report the outcome of each step concisely. If any step fails in a way
that blocks later steps, stop and explain what went wrong before
continuing.

## 1. Prereq checks

Verify these tools exist. Report each as present/missing. Don't stop on
missing tools yet — collect all the missing ones and list them together
at the end of this step, along with install commands. Tools:

- `uv` — Python package manager, required. Install with
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `bun` — TypeScript runtime used by the channel server, required.
  Install with `curl -fsSL https://bun.sh/install | bash`
- `nvidia-smi` — NVIDIA driver — optional but strongly recommended.
  If missing, warn the user that the daemon will need
  `VOICE_STT_COMPUTE_DEVICE=cpu` in their config and will be much
  slower.
- `dpkg` (or equivalent) — used to check for `libportaudio2` below.

Also check: is the current user in the `input` group? Run `id -nG` and
look for `input`. Report the result. If missing, the PTT listener won't
be able to read `/dev/input/event*` and voice dictation will silently
fail. Tell the user to run `sudo usermod -aG input $USER` and log out
and back in.

Check libportaudio2 on Debian/Ubuntu:
```
dpkg -l libportaudio2 2>/dev/null | grep -q '^ii' && echo "libportaudio2: ok" || echo "libportaudio2: missing (sudo apt install libportaudio2)"
```

If you detect a non-Debian distro (check `/etc/os-release`), adapt the
check to that package manager (dnf/rpm on Fedora, pacman on Arch,
etc.) or just tell the user to install portaudio via their package
manager and move on.

## 2. Install Python dependencies

Run in the plugin's cached directory:

```
cd "${CLAUDE_PLUGIN_ROOT}" && uv sync
```

Report the output. On first run this downloads the faster-whisper,
CTranslate2, sounddevice, evdev, and cuBLAS/cuDNN wheels (~200 MB
total). Subsequent runs are fast because `uv`'s global wheel cache
lives at `~/.cache/uv` and persists across plugin reinstalls.

If `uv sync` fails, stop and report the error. Common causes: no
Python 3.10+ available (uv will auto-install it, but needs network
access), or a pre-existing incompatible `.venv` directory in the
plugin root (safe to `rm -rf "${CLAUDE_PLUGIN_ROOT}/.venv"` and retry).

## 3. Install TypeScript dependencies

Run in the channel subdir of the plugin:

```
cd "${CLAUDE_PLUGIN_ROOT}/channel" && bun install --frozen-lockfile
```

Should be fast — the only dependency is `@modelcontextprotocol/sdk`.
Report the output.

## 4. Create the user config file

The config file lives at `$XDG_CONFIG_HOME/voice-stt/config`, which
defaults to `$HOME/.config/voice-stt/config` if `XDG_CONFIG_HOME` is
unset. Determine the path once and use it consistently for the rest of
this step.

Check whether the file exists:

- **If it exists**: tell the user the path and that you're preserving
  their existing config. Run `wc -l` on it to confirm it's non-empty.
  Skip the template copy — never overwrite an existing config file.

- **If it does not exist**: create the parent directory, copy the
  template from `${CLAUDE_PLUGIN_ROOT}/config.template` to the user
  config path, and tighten permissions to 0600:
  ```
  mkdir -p "$(dirname "$CONFIG_PATH")"
  cp "${CLAUDE_PLUGIN_ROOT}/config.template" "$CONFIG_PATH"
  chmod 600 "$CONFIG_PATH"
  ```
  Then tell the user:
  - the path of the config file
  - what values they might want to edit:
    - `VOICE_STT_MODEL` — model size (medium.en default is good for
      an 8 GB GPU; use small.en for smaller GPUs)
    - `VOICE_STT_PULSE_SOURCE` — uncomment and set this if they see
      alternating-silence dictation failures (means their default
      PipeWire source is a noise-cancel virtual node; bypass it with
      the raw hardware source name from `wpctl status`)
    - `VOICE_STT_PTT_KEY` — which evdev key the PTT thread watches
      for (default KEY_F20)

## 5. Migration from legacy ~/projects/voice-stt/.env

Check whether `$HOME/projects/voice-stt/.env` exists AND we just
created the user config in step 4 (i.e. this is a fresh install that
replaces an old manual one). If both are true, offer to copy the
user's previous overrides over.

```
if [ -f "$HOME/projects/voice-stt/.env" ]; then
    echo "Found legacy config at $HOME/projects/voice-stt/.env"
    diff "${CLAUDE_PLUGIN_ROOT}/config.template" "$HOME/projects/voice-stt/.env"
fi
```

Ask the user if they want you to merge the old values into the new
config file. If yes, append the non-default lines from the old file to
the new config, preserving comments. Don't delete the old file — leave
it in place so the user can verify the migration manually.

## 6. Migration from legacy systemd user units

Check for `$HOME/.config/systemd/user/voice-sttd.service` — if present,
the user was on the previous architecture that ran voice-stt as a
separate systemd-managed daemon. That's been replaced by the plugin
spawning the daemon as a subprocess.

```
if [ -f "$HOME/.config/systemd/user/voice-sttd.service" ]; then
    echo "Found legacy systemd units — cleaning up..."
    systemctl --user disable --now voice-sttd.service voice-stt-ptt.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/voice-sttd.service"
    rm -f "$HOME/.config/systemd/user/voice-stt-ptt.service"
    systemctl --user daemon-reload
    echo "Removed legacy systemd units."
fi
```

Tell the user what you cleaned up. If they had `loginctl enable-linger`
set for voice-stt, remind them it's no longer needed but leaving it
enabled is harmless.

## 7. Validate the install

Confirm the end state:

```
ls -l "$CONFIG_PATH"
ls -l "${CLAUDE_PLUGIN_ROOT}/.venv/bin/voice-sttd" 2>/dev/null || echo "voice-sttd entrypoint missing"
ls -l "${CLAUDE_PLUGIN_ROOT}/channel/node_modules" 2>/dev/null | head -1 || echo "channel node_modules missing"
```

All three should exist. If any is missing, report which step failed.

## 8. Print next steps

Tell the user:

1. **To start using voice dictation now**, they need to launch Claude
   Code with the channel enabled. Custom channels are gated behind
   `--dangerously-load-development-channels` during the research preview:
   ```
   claude --dangerously-load-development-channels plugin:dictate@maxinertia
   ```
   They can alias that in their shell rc if they want muscle memory.

2. **To bind a hardware button to the PTT key**, they need to map a
   physical key on their keyboard/mouse to the evdev key named in
   `VOICE_STT_PTT_KEY` (default `KEY_F20`). The README covers several
   key remappers: input-remapper (GUI), xremap (config-based), kmonad
   (layer-based). None of this can be automated — it depends on their
   hardware.

3. **The daemon starts automatically** when Claude Code launches with
   the channel plugin active. Nothing to manage — no `systemctl`, no
   background services. Exiting Claude Code cleans everything up.

4. **If dictation doesn't work after setup**, the first thing to check
   is Claude Code's debug log at `~/.claude/debug/<session-id>.txt` —
   the channel server and daemon both forward their stderr there.
