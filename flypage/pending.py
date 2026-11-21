"""Deletions on a scanner that was unplugged before they could be done.

Stored in ``~/.local/state/flypage/pending-deletions.json`` and applied the
next time that scanner is plugged in. A file is only deleted if it still
has the same size and modification time, so a new scan that reused the
name is never lost.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path

from flypage import paths

log = logging.getLogger(__name__)


def _file() -> Path:
    return paths.state_dir() / "pending-deletions.json"


@contextlib.contextmanager
def _locked():
    """Exclusive access: the daemon and processing windows are separate processes."""
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError, ValueError) as err:
            log.warning("ignoring unreadable %s: %s", path, err)
            entries = []
        state = {"entries": entries, "dirty": False}
        yield state
        if state["dirty"]:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state["entries"], indent=1), encoding="utf-8")
            os.replace(tmp, path)


def add(scanner: str, relpath: str, size: int, mtime_ns: int) -> None:
    entry = {"scanner": scanner, "relpath": relpath, "size": size, "mtime_ns": mtime_ns}
    with _locked() as state:
        if entry not in state["entries"]:
            state["entries"].append(entry)
            state["dirty"] = True


def entries(scanner: str | None = None) -> list[dict]:
    with _locked() as state:
        return [e for e in state["entries"] if scanner is None or e["scanner"] == scanner]


def delete_if_same(path: Path, size: int, mtime_ns: int) -> bool:
    """Delete ``path`` if it is still the file that was imported; True if it is gone."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return True
    if st.st_size != size or st.st_mtime_ns != mtime_ns:
        log.info("%s changed since it was imported: not deleted", path)
        return False
    path.unlink()
    return True


def apply(scanner: str, mount_point: Path) -> int:
    """Do the pending deletions of ``scanner``, now mounted at ``mount_point``; return how many."""
    deleted = 0
    with _locked() as state:
        keep = []
        for entry in state["entries"]:
            if entry["scanner"] != scanner:
                keep.append(entry)
                continue
            path = mount_point / entry["relpath"]
            try:
                existed = path.exists()
                delete_if_same(path, entry["size"], entry["mtime_ns"])
                deleted += existed and not path.exists()
            except OSError as err:
                log.warning("cannot delete %s: %s", path, err)
                keep.append(entry)  # try again next time
        if len(keep) != len(state["entries"]):
            state["entries"] = keep
            state["dirty"] = True
    return deleted
