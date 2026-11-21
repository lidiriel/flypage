"""Per-user directories (XDG base directory spec)."""

import os
from pathlib import Path

APP = "flypage"


def _xdg(variable: str, fallback: str) -> Path:
    base = os.environ.get(variable, "")
    if not os.path.isabs(base):  # the spec says to ignore relative paths
        return Path.home() / fallback / APP
    return Path(base) / APP


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def contract_home(path: str) -> str:
    """Show /home/me/Scans as ~/Scans (and store it that way: it survives a renamed home)."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def work_dir() -> Path:
    """Scratch space for intermediate PDFs: on disk, not in a possibly small tmpfs."""
    path = cache_dir() / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path
