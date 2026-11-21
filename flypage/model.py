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
    chain = GObject.Property(type=int, default=0)  # 0 = not chained
    dest_icon = GObject.Property(type=str, default="")  # destination icon while queued

    def __init__(self, path: Path, pages: int = 1) -> None:
        super().__init__()
        self.id = next(_ids)
        self.path = path
        self.pages = pages

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def sortable(self) -> bool:
        """Can be chained, moved, deleted or sent (not while its OCR is queued)."""
        return self.state != QUEUED

    def __repr__(self) -> str:
        return f"<ImportedFile {self.id} {self.name} {self.state} chain={self.chain}>"
