"""Copy a scanner's files to a staging folder, and delete them on the scanner later.

The staging folder (``~/.cache/flypage/incoming/<time>-<scanner>/``) holds the
copies plus ``manifest.json``, which maps each copy back to its file on the
scanner. The processing window, a separate process, uses it to delete the
originals: right away if the scanner is still mounted, else at the next plug
(see ``pending``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from flypage import paths, pending
from flypage.config import Scanner

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = (".png", ".jpg", ".jpeg", ".pdf")
MANIFEST = "manifest.json"


class ImportFailed(Exception):
    pass


def find_files(folder: Path) -> list[Path]:
    """PNG, JPEG and PDF files under ``folder``, skipping hidden folders such as .Trash-1000."""
    found = []
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            found.append(path)
    return found


@dataclass
class Entry:
    staging: str  # path of the copy, relative to the staging folder
    relpath: str  # path on the scanner, relative to its mount point
    size: int
    mtime_ns: int


@dataclass
class Manifest:
    scanner: str  # profile name
    mount_point: str
    delete_after_import: bool
    entries: list[Entry] = field(default_factory=list)

    def save(self, folder: Path) -> None:
        tmp = folder / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        os.replace(tmp, folder / MANIFEST)

    @classmethod
    def load(cls, folder: Path) -> Manifest:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        data["entries"] = [Entry(**e) for e in data["entries"]]
        return cls(**data)


def incoming_dir() -> Path:
    return paths.cache_dir() / "incoming"


def import_files(
    mount_point: Path,
    scanner: Scanner,
    cancelled: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Copy every file of ``scanner`` to a new staging folder and return it.

    Raises ``ImportFailed`` (and leaves nothing behind) if the scanner is
    unplugged or ``cancelled`` is set.
    """
    source = mount_point / scanner.source_dir
    if not source.is_dir():
        raise ImportFailed(f"{source} does not exist")
    files = find_files(source)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", scanner.name).strip("-") or "scanner"
    staging = incoming_dir() / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    staging.mkdir(parents=True, exist_ok=False)
    manifest = Manifest(scanner.name, str(mount_point), scanner.delete_after_import)
    try:
        for done, path in enumerate(files):
            if cancelled is not None and cancelled.is_set():
                raise ImportFailed("cancelled")
            st = path.stat()
            relpath = path.relative_to(mount_point).as_posix()
            copy = staging / path.relative_to(source)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
            manifest.entries.append(Entry(copy.relative_to(staging).as_posix(), relpath, st.st_size, st.st_mtime_ns))
            if progress is not None:
                progress(done + 1, len(files))
        manifest.save(staging)
    except (OSError, ImportFailed) as err:
        shutil.rmtree(staging, ignore_errors=True)
        raise ImportFailed(str(err)) from err
    return staging


class ScannerFiles:
    """The originals, on the scanner, of the copies in a staging folder."""

    def __init__(self, staging: Path, is_mounted: Callable[[str], bool] = os.path.ismount) -> None:
        self.staging = staging
        self.manifest = Manifest.load(staging)
        self._by_copy = {str(staging / e.staging): e for e in self.manifest.entries}
        self._is_mounted = is_mounted

    @property
    def scanner(self) -> str:
        return self.manifest.scanner

    @property
    def delete_after_import(self) -> bool:
        return self.manifest.delete_after_import

    def files(self) -> list[Path]:
        return [self.staging / e.staging for e in self.manifest.entries]

    def delete_original(self, copy: Path) -> None:
        """Delete the scanner's file for ``copy``: now if mounted, else at the next plug.

        Raises ``OSError`` if the scanner is mounted but the file cannot be deleted.
        """
        entry = self._by_copy.get(str(copy))
        if entry is None:
            return
        mount = self.manifest.mount_point
        if not self._is_mounted(mount):
            log.info("%s unplugged: deletion of %s postponed", self.scanner, entry.relpath)
            pending.add(self.scanner, entry.relpath, entry.size, entry.mtime_ns)
            return
        pending.delete_if_same(Path(mount) / entry.relpath, entry.size, entry.mtime_ns)

    def cleanup(self) -> None:
        """Remove the staging folder (the scanner keeps the files not yet deleted)."""
        shutil.rmtree(self.staging, ignore_errors=True)
