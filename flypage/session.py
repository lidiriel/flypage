"""What the processing window does, without the widgets (PLAN.md §6).

The session owns the list of files shown in the grid, the chains, and the
pipeline. Sending files to a destination queues them for OCR right away;
each one leaves the list when its PDF is written. All methods run on the
GTK main thread; pipeline callbacks are brought back to it with idle_add.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

from gi.repository import Gio, GLib, GObject

from flypage.config import Config, Destination
from flypage.model import FAILED, IDLE, QUEUED, ImportedFile
from flypage.importer import ScannerFiles
from flypage.ocr import OcrEngine
from flypage.naming import TemplateError
from flypage.pipeline import Document, Pipeline, Progress, Result, document_name, page_count

log = logging.getLogger(__name__)

ALL_SAME, PER_FILE = "all_same", "per_file"


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
        sort_mode: str | None = None,
        originals: ScannerFiles | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.sort_mode = sort_mode or cfg.processing.sort_mode
        self.remove_sources = remove_sources
        self.engine = engine
        self.originals = originals  # the scanner's files, when imported from one
        self.store = Gio.ListStore(item_type=ImportedFile)
        for path in files:
            self.store.append(ImportedFile(path, page_count(path)))
        self.results: list[Result] = []
        self._pipeline: Pipeline | None = None
        self._pending = 0
        self._next_chain = 1

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

    def chain_members(self, chain: int) -> list[ImportedFile]:
        return [item for item in self.store if chain and item.chain == chain]

    def expand(self, items: list[ImportedFile]) -> list[ImportedFile]:
        """``items`` plus the rest of their chains, in grid order."""
        chains = {item.chain for item in items if item.chain}
        wanted = {item.id for item in items}
        return [item for item in self.store if item.id in wanted or item.chain in chains]

    # --- chains ----------------------------------------------------------------

    def toggle_chain(self, items: list[ImportedFile]) -> None:
        """Chain ``items`` into one PDF, or unchain them if they are exactly one chain already."""
        items = [i for i in items if i.sortable]
        chains = {i.chain for i in items}
        if len(chains) == 1 and 0 not in chains and set(self.chain_members(chains.pop())) == set(items):
            for item in items:
                item.chain = 0
            return
        if len(items) < 2:
            return
        chain = self._next_chain
        self._next_chain += 1
        old_chains = {i.chain for i in items if i.chain}
        for item in items:
            item.chain = chain
        for old in old_chains:
            self._dissolve_if_single(old)
        self._gather(chain)

    def remove_from_chain(self, item: ImportedFile) -> None:
        old, item.chain = item.chain, 0
        self._dissolve_if_single(old)

    def _dissolve_if_single(self, chain: int) -> None:
        members = self.chain_members(chain)
        if len(members) == 1:
            members[0].chain = 0

    def _gather(self, chain: int) -> None:
        """Make a chain's files adjacent in the grid, at the place of its first one."""
        members = self.chain_members(chain)
        first = self._position(members[0])
        rest = members[1:]
        for item in rest:
            self.store.remove(self._position(item))
        for offset, item in enumerate(rest, start=1):
            self.store.insert(first + offset, item)

    # --- grid order ------------------------------------------------------------

    def move(self, items: list[ImportedFile], before: ImportedFile | None) -> None:
        """Move ``items`` (in their current order) before ``before``, or to the end."""
        items = [i for i in self.expand(items) if i.sortable]
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
            if item.chain:
                self.remove_from_chain(item)
            self.store.remove(self._position(item))
        self._check_finished()
        return errors

    # --- send to a destination -------------------------------------------------

    def items_to_send(self, items: list[ImportedFile]) -> list[ImportedFile]:
        """What a drop of ``items`` sends, depending on the mode."""
        if self.sort_mode == ALL_SAME:
            return [i for i in self.store if i.sortable]
        return [i for i in self.expand(items) if i.sortable]

    def send(self, items: list[ImportedFile], dest: Destination) -> int:
        """Queue the files for OCR into ``dest``; return the number of documents.

        Raises ``OcrError`` if OCR cannot run at all (engine or language missing).
        """
        items = self.items_to_send(items)
        if not items:
            return 0
        if self._pipeline is None:
            self._pipeline = Pipeline(
                self.cfg, self.engine, remove_sources=self.remove_sources, progress=self._progress_from_thread
            )
        documents = 0
        for group in self._documents(items):
            doc = Document(
                [i.path for i in group], dest.expanded_path(), dest_name=dest.name, template=dest.template
            )
            future = self._pipeline.submit(doc)
            for item in group:
                item.state = QUEUED
                item.error = ""
                item.dest_icon = dest.icon
            self._pending += 1
            documents += 1
            future.add_done_callback(lambda f, g=group: GLib.idle_add(self._on_done, g, f))
        return documents

    def _documents(self, items: list[ImportedFile]) -> list[list[ImportedFile]]:
        """One group per chain and per unchained file, in grid order."""
        groups: list[list[ImportedFile]] = []
        by_chain: dict[int, list[ImportedFile]] = {}
        for item in items:
            if item.chain:
                if item.chain not in by_chain:
                    by_chain[item.chain] = []
                    groups.append(by_chain[item.chain])
                by_chain[item.chain].append(item)
            else:
                groups.append([item])
        return groups

    def _progress_from_thread(self, progress: Progress) -> None:
        GLib.idle_add(self.emit, "progress", progress.pages_done, progress.pages_total, progress.current)

    def _on_done(self, group: list[ImportedFile], future: Future) -> bool:
        result: Result = future.result()
        self._pending -= 1
        self.results.append(result)
        if result.ok:
            for item in group:
                found, position = self.store.find(item)
                if found:
                    self.store.remove(position)
                self._delete_original_after_import(item)
        else:
            for item in group:
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

    def preview_name(self, items: list[ImportedFile], dest: Destination) -> str | None:
        """File name the selection would get in ``dest`` (one file or one chain), else None."""
        groups = self._documents(self.expand(items)) if items else []
        if len(groups) != 1:
            return None
        group = groups[0]
        doc = Document([i.path for i in group], dest.expanded_path(), dest_name=dest.name, template=dest.template)
        n = self._pipeline.next_number(doc.dest_dir) if self._pipeline else 1
        when = self._pipeline.started if self._pipeline else datetime.now()
        try:
            return document_name(self.cfg, doc, n, sum(i.pages for i in group), when) + ".pdf"
        except TemplateError:
            return None

    # --- end -------------------------------------------------------------------

    def cancel(self) -> None:
        """Stop the running OCR and drop the queue; written PDFs stay where they are."""
        if self._pipeline is not None:
            self._pipeline.cancel()
        self.close()

    def close(self) -> None:
        if self._pipeline is not None:
            self._pipeline.close()
            self._pipeline = None
        if self.originals is not None:
            # Copies not turned into PDFs are dropped; the scanner still has them.
            self.originals.cleanup()

    def outputs(self) -> list[Path]:
        return [r.output for r in self.results if r.ok]
