"""Where cao keeps its own data — one definition for the four modules that need it.

The fallback has to agree with plugin/hooks/session_start.sh, which installs the backend
under `${HOME}/.config/cao`. `Path.home()` does not agree on Windows: it reads USERPROFILE
and ignores HOME, so a shell whose HOME points elsewhere leaves the hook installing to one
directory and every reader looking in another.

plugin/scripts/cao-companion.py carries a mirror of this (it cannot import cao).
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """$HOME if set, else the OS's idea of it.

    POSIX expanduser() already prefers $HOME, so this is a no-op there; it only adds
    that preference on Windows, where ntpath consults USERPROFILE and drops HOME.
    """
    env_home = os.environ.get("HOME")
    return Path(env_home) if env_home else Path.home()


def plugin_data_dir() -> Path:
    """CAO_PLUGIN_DATA if set, else ~/.config/cao — the base session_start.sh installs into."""
    env_data = os.environ.get("CAO_PLUGIN_DATA")
    return Path(env_data) if env_data else home() / ".config" / "cao"
