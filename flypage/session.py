"""What the processing window does, without the widgets (PLAN.md §6, §9).

The session owns the list of files shown in the grid and the pipeline.
Sending files to a destination queues each of them for OCR right away, as
its own document; each one leaves the list when its output is written. All
methods run on the GTK main thread; pipeline callbacks are brought back to
it with idle_add.

Three choices of the window apply to the next send: ``convert_images``
(off: images are written as they are, without OCR), ``copy`` (keep the
originals) and ``languages`` (OCR languages).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from gi.repository import Gio, GLib, GObject

from flypage.config import Config, Destination
from flypage.model import FAILED, IDLE, QUEUED, ImportedFile
from flypage.importer import ScannerFiles
from flypage.ocr import OcrEngine
from flypage.naming import TemplateError
from flypage.pipeline import Document, Pipeline, Progress, Result, document_name, page_count, pdf_has_text
from flypage.progress import Estimator, Snapshot, Speed

log = logging.getLogger(__name__)


class Session(GObject.Object):
    __gtype_name__ = "FlypageSession"
    __gsignals__ = {
        # pages done, pages queued in total, file being written
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (int, int, str)),
        # a document ended (Result), successfully or not
        "document-done": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # nothing left to sort and nothing running
        "finished": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # something went wrong that the user should know about (message)
        "warning": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self,
        cfg: Config,
        files: list[Path],
        *,
        remove_sources: bool = True,
        engine: OcrEngine | None = None,
        originals: ScannerFiles | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.remove_sources = remove_sources  # in Move mode
        self.engine = engine
        self.originals = originals  # the scanner's files, when imported from one
        self.convert_images = True
        self.copy = False  # True: the originals stay where they are
        self.languages = list(cfg.ocr.languages)
        self.store = Gio.ListStore(item_type=ImportedFile)
        for path in files:
            is_pdf = path.suffix.lower() == ".pdf"
            self.store.append(ImportedFile(path, page_count(path), digital=is_pdf and pdf_has_text(path)))
        self.results: list[Result] = []
        self._pipeline: Pipeline | None = None
        self._pending = 0
        self._speed = Speed.load()
        self._estimator: Estimator | None = None
        self._items_by_doc: dict[int, ImportedFile] = {}  # id(Document) → its file

    # --- queries ---------------------------------------------------------------

    @property
    def items(self) -> list[ImportedFile]:
        return list(self.store)

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def by_id(self, ids: list[int]) -> list[ImportedFile]:
        wanted = set(ids)
        return [item for item in self.store if item.id in wanted]

    # --- grid order ------------------------------------------------------------

    def move(self, items: list[ImportedFile], before: ImportedFile | None) -> None:
        """Move ``items`` (in their current order) before ``before``, or to the end."""
        items = [i for i in self.store if i in items and i.sortable]
        if not items or before in items:
            return
        for item in items:
            self.store.remove(self._position(item))
        position = self._position(before) if before is not None else self.store.get_n_items()
        self.store.splice(position, 0, items)

    def _position(self, item: ImportedFile) -> int:
        found, position = self.store.find(item)
        if not found:
            raise ValueError(f"{item!r} is not in the grid")
        return position

    # --- delete ----------------------------------------------------------------

    def delete(self, items: list[ImportedFile]) -> list[str]:
        """Delete the files (and their originals on the scanner) and remove them from the grid.

        Returns error messages.
        """
        errors = []
        for item in [i for i in items if i.sortable]:
            try:
                if self.originals is not None:
                    self.originals.delete_original(item.path)
                item.path.unlink(missing_ok=True)
            except OSError as err:
                errors.append(f"{item.name}: {err.strerror or err}")
                continue
            self.store.remove(self._position(item))
        self._check_finished()
        return errors

    # --- send to a destination -------------------------------------------------

    def send(self, items: list[ImportedFile], dest: Destination) -> int:
        """Queue each file as its own document into ``dest``; return the number queued.

        Raises ``OcrError`` if OCR cannot run at all (engine or language missing).
        """
        items = [i for i in self.store if i in items and i.sortable]  # grid order
        if not items:
            return 0
        if self._pipeline is None:
            self._pipeline = Pipeline(
                self.cfg, self.engine, remove_sources=self.remove_sources, progress=self._progress_from_thread,
                check=False,  # checked below, only if some file needs OCR
            )
        if any(not self.passthrough(i) for i in items):
            self._pipeline.engine.check(self.languages)
        if self._estimator is None:
            self._estimator = Estimator(self._speed, self._pipeline.workers)
        for item in items:
            doc = self._document(item, dest)
            self._items_by_doc[id(doc)] = item
            future = self._pipeline.submit(doc)
            self._estimator.queue(item.id, item.pages, doc.passthrough)
            item.state = QUEUED
            item.error = ""
            item.dest_icon = dest.icon
            item.progress = -1.0
            self._pending += 1
            future.add_done_callback(lambda f, it=item: GLib.idle_add(self._on_done, it, f))
        return len(items)

    def estimate(self) -> Snapshot | None:
        """Estimated progress now (None when nothing is queued); updates each file's ``progress``."""
        if self._estimator is None or self._estimator.idle:
            return None
        snapshot = self._estimator.snapshot(time.monotonic())
        for item in self.store:
            if item.state == QUEUED and item.id in snapshot.documents:
                item.progress = snapshot.documents[item.id]
        return snapshot

    def passthrough(self, item: ImportedFile) -> bool:
        """True if sending ``item`` now writes it as it is, without OCR."""
        return item.digital or (not self.convert_images and not item.is_pdf)

    def _document(self, item: ImportedFile, dest: Destination) -> Document:
        return Document(
            [item.path], dest.expanded_path(), dest_name=dest.name, template=dest.template,
            table_layout=dest.table_layout,
            languages=list(self.languages), convert_images=self.convert_images,
            remove_sources=self.remove_sources and not self.copy,
        )  # fmt: skip

    def _progress_from_thread(self, progress: Progress) -> None:
        if progress.started is not None:
            GLib.idle_add(self._on_started, id(progress.started), time.monotonic())
        GLib.idle_add(self.emit, "progress", progress.pages_done, progress.pages_total, progress.current)

    def _on_started(self, doc_id: int, when: float) -> bool:
        item = self._items_by_doc.get(doc_id)
        if item is not None and self._estimator is not None:
            self._estimator.start(item.id, when)
            item.progress = 0.0
        return GLib.SOURCE_REMOVE

    def _on_done(self, item: ImportedFile, future: Future) -> bool:
        result: Result = future.result()
        self._pending -= 1
        self.results.append(result)
        self._items_by_doc.pop(id(result.document), None)
        item.progress = -1.0
        if self._estimator is not None:
            self._estimator.finish(item.id, counted=not result.cancelled)
        if result.ok and result.ocr_seconds > 0:
            self._speed.record(item.pages, result.ocr_seconds)
        if result.ok:
            found, position = self.store.find(item)
            if found:
                self.store.remove(position)
            if result.document.remove_sources:  # Move: the scanner's file goes too
                self._delete_original_after_import(item)
        else:
            item.state = FAILED if not result.cancelled else IDLE
            item.error = result.error or ""
            item.dest_icon = ""
        self.emit("document-done", result)
        self._check_finished()
        return GLib.SOURCE_REMOVE

    def _delete_original_after_import(self, item: ImportedFile) -> None:
        if self.originals is None or not self.originals.delete_after_import:
            return
        try:
            self.originals.delete_original(item.path)
        except OSError as err:
            log.warning("cannot delete %s on the scanner: %s", item.name, err)
            self.emit("warning", f"{item.name}: {err.strerror or err}")

    def _check_finished(self) -> None:
        if self.store.get_n_items() == 0 and not self.busy:
            self.close()
            self.emit("finished")

    def preview_name(self, item: ImportedFile, dest: Destination) -> str | None:
        """File name ``item`` would get in ``dest`` if sent now, or None."""
        doc = self._document(item, dest)
        n = self._pipeline.next_number(doc.dest_dir) if self._pipeline else 1
        when = self._pipeline.started if self._pipeline else datetime.now()
        try:
            return document_name(self.cfg, doc, n, item.pages, when) + doc.output_suffix()
        except TemplateError:
            return None

    # --- end -------------------------------------------------------------------

    def cancel(self) -> None:
        """Stop the running OCR and drop the queue; written files stay where they are."""
        if self._pipeline is not None:
            self._pipeline.cancel()
        self.close()

    def close(self) -> None:
        if self._pipeline is not None:
            self._pipeline.close()
            self._pipeline = None
        if self.originals is not None:
            # Copies not sent are dropped; the scanner still has them.
            self.originals.cleanup()

    def outputs(self) -> list[Path]:
        return [r.output for r in self.results if r.ok]
