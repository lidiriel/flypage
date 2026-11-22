"""Objects shown in the processing window's grid."""

from __future__ import annotations

import itertools
from pathlib import Path

from gi.repository import GObject

IDLE = "idle"  # waiting to be sorted
QUEUED = "queued"  # sent to a destination, OCR pending or running
FAILED = "failed"  # OCR failed; can be sent again or deleted

_ids = itertools.count(1)


class ImportedFile(GObject.Object):
    """One scanned file. It leaves the grid once its PDF has been written."""

    __gtype_name__ = "FlypageImportedFile"

    state = GObject.Property(type=str, default=IDLE)
    error = GObject.Property(type=str, default="")
    dest_icon = GObject.Property(type=str, default="")  # destination icon while queued
    # While queued: -1 = waiting for a worker, else the estimated 0…1 of its OCR.
    progress = GObject.Property(type=float, default=-1.0)

    def __init__(self, path: Path, pages: int = 1, digital: bool = False) -> None:
        super().__init__()
        self.id = next(_ids)
        self.path = path
        self.pages = pages
        self.is_pdf = path.suffix.lower() == ".pdf"
        self.digital = digital  # a PDF that already has text: filed without OCR

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def sortable(self) -> bool:
        """Can be moved, deleted or sent (not while its OCR is queued)."""
        return self.state != QUEUED

    def __repr__(self) -> str:
        return f"<ImportedFile {self.id} {self.name} {self.state}>"
