"""Load $XDG_CONFIG_HOME/voice-stt/config into os.environ.

Config file format
------------------
Plain KEY=VALUE lines, one per line. Comments start with ``#``. Blank lines
are ignored. Values may be optionally quoted with single or double quotes
(the quotes are stripped). No shell expansion, no multi-line values, no
``export`` prefix.

Example::

    # ~/.config/voice-stt/config
    VOICE_STT_MODEL=medium.en
    VOICE_STT_PULSE_SOURCE=alsa_input.pci-0000_09_00.4.analog-stereo
    VOICE_STT_PTT_KEY=KEY_F20

Precedence
----------
``load()`` uses ``os.environ.setdefault``, which means shell env vars
already set (e.g. exported in ``~/.bashrc`` or passed on the command
line with ``VAR=x cmd``) take precedence over the config file. The
config file only fills in values that aren't already set.

Why ``setdefault`` instead of always overwriting: one-off overrides like
``VOICE_STT_MODEL=small.en voice-sttd`` should still work without editing
the config file. Shell env wins.
"""

from __future__ import annotations

import os
from pathlib import Path


def config_path() -> Path:
    """Return the canonical XDG config file path.

    Respects ``$XDG_CONFIG_HOME`` if set, falling back to
    ``$HOME/.config`` per the XDG Base Directory spec.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "voice-stt" / "config"


def load(path: Path | None = None) -> int:
    """Parse the config file and populate ``os.environ``.

    Returns the number of keys that were actually set (useful for logging).
    Silently does nothing if the file doesn't exist — absence of a config
    file is a normal state.

    Malformed lines are skipped with no error. Keys that don't match a
    conservative identifier pattern (``^[A-Za-z_][A-Za-z0-9_]*$``) are
    silently dropped to avoid surprise env-var injection from a typo in
    the config file.
    """
    p = path or config_path()
    if not p.is_file():
        return 0

    loaded = 0
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return 0

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # strip an optional leading `export ` prefix so files that were
        # originally shell-sourced still parse
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key.isidentifier():
            continue
        # strip surrounding matched quotes (but only matched pairs)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key in os.environ:
            # shell env wins — don't overwrite an explicit user setting
            continue
        os.environ[key] = val
        loaded += 1

    return loaded
