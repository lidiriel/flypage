"""Turn scanned files into searchable PDFs (PLAN.md §7).

For each ``Document`` (one file, or a chain of files merged into one PDF):
normalise every file to PDF, merge, OCR, write ``<dest>/<name>.pdf``, and
optionally remove the originals. A failing document does not stop the others.

Documents are queued with ``Pipeline.submit()`` while others are running, which
is what the processing window does on each drag-and-drop. ``run()`` is the
batch shortcut used by the command line.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import img2pdf
import pikepdf
from PIL import Image

from flypage import naming, paths
from flypage.cancel import Cancelled, CancelToken
from flypage.config import Config
from flypage.i18n import _
from flypage.ocr import OcrEngine, get_engine

log = logging.getLogger(__name__)

IMAGE_FORMATS = ("PNG", "JPEG")
_EXIF_IFD = 0x8769
_EXIF_DATETIME_ORIGINAL = 0x9003


class UnsupportedFile(Exception):
    pass


@dataclass
class Document:
    sources: list[Path]  # page order
    dest_dir: Path
    dest_name: str = ""  # value of {dest}; defaults to the folder name
    template: str = ""  # overrides the configured file name template
    scanner: str = ""  # value of {scanner}


@dataclass
class Result:
    document: Document
    output: Path | None = None
    error: str | None = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.output is not None


@dataclass
class Progress:
    """Snapshot sent to the progress callback, from any worker thread."""

    pages_done: int
    pages_total: int
    documents_done: int
    documents_total: int
    current: str = ""  # file name being written, empty when idle
    finished: Result | None = None  # set when a document has just ended


@dataclass
class _Job:
    document: Document
    stem: str  # output name without extension
    pages: int


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        engine: OcrEngine | None = None,
        *,
        remove_sources: bool = False,
        workers: int | None = None,
        progress: Callable[[Progress], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine or get_engine(cfg.ocr.engine)
        self.engine.check(cfg.ocr.languages)
        self.remove_sources = remove_sources
        self.started = datetime.now()
        self.cancel_token = CancelToken()
        self._progress_cb = progress
        self._lock = threading.Lock()
        self._sequence: dict[Path, int] = {}
        self._pages_done = self._pages_total = 0
        self._docs_done = self._docs_total = 0
        limit = min(filter(None, (workers, self.engine.max_workers, os.cpu_count() or 1)))
        self._executor = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="flypage-ocr")

    # --- public API ----------------------------------------------------------

    def submit(self, document: Document) -> Future[Result]:
        """Queue ``document``. Raises ``naming.TemplateError`` if its name cannot be built."""
        if not document.sources:
            raise ValueError("a document needs at least one source file")
        pages = sum(page_count(p) for p in document.sources)
        with self._lock:
            dest_key = document.dest_dir.expanduser().resolve()
            n = self._sequence.get(dest_key, 0) + 1
            stem = self._name(document, n, pages)  # may raise: then n is not used up
            self._sequence[dest_key] = n
            self._pages_total += pages
            self._docs_total += 1
        return self._executor.submit(self._process, _Job(document, stem, pages))

    def run(self, documents: list[Document]) -> list[Result]:
        futures = [self.submit(d) for d in documents]
        return [f.result() for f in futures]

    def next_number(self, dest_dir: Path) -> int:
        """The %n the next document sent to ``dest_dir`` will get."""
        with self._lock:
            return self._sequence.get(dest_dir.expanduser().resolve(), 0) + 1

    def output_name(self, document: Document) -> str:
        """The file name the next submit of ``document`` would get (before collision suffixes)."""
        with self._lock:
            n = self._sequence.get(document.dest_dir.expanduser().resolve(), 0) + 1
        pages = sum(page_count(p) for p in document.sources)
        return self._name(document, n, pages) + ".pdf"

    def cancel(self) -> None:
        """Stop running OCR and drop queued documents; their results say ``cancelled``."""
        self.cancel_token.cancel()

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def __enter__(self) -> Pipeline:
        return self

    def __exit__(self, *exc) -> None:
        if exc[0] is not None:
            self.cancel()
        self.close()

    # --- worker --------------------------------------------------------------

    def _name(self, document: Document, n: int, pages: int) -> str:
        return document_name(self.cfg, document, n, pages, self.started)

    def _process(self, job: _Job) -> Result:
        result = Result(job.document)
        try:
            self.cancel_token.check()
            self._report(current=job.stem + ".pdf")
            result.output = self._convert(job)
        except Cancelled:
            result.cancelled = True
            result.error = _("Cancelled")
        except Exception as err:  # one bad document must not stop the others
            log.debug("processing %s failed", job.document.sources, exc_info=True)
            result.error = str(err) or type(err).__name__
        else:
            if self.remove_sources:
                for src in job.document.sources:
                    try:
                        src.unlink()
                    except OSError as err:
                        log.warning("cannot remove %s: %s", src, err)
        self._report(finished=result, pages=job.pages)
        return result

    def _convert(self, job: _Job) -> Path:
        dest_dir = job.document.dest_dir.expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="doc-", dir=paths.work_dir()) as tmp:
            tmp_dir = Path(tmp)
            pdfs = [_to_pdf(src, tmp_dir / f"{i:04}.pdf") for i, src in enumerate(job.document.sources)]
            self.cancel_token.check()
            merged = pdfs[0] if len(pdfs) == 1 else _merge(pdfs, tmp_dir / "merged.pdf")

            # OCR straight into the destination folder under a hidden name, so
            # the final rename is atomic (no half-written PDF ever visible).
            fd, part = tempfile.mkstemp(prefix=".flypage-", suffix=".pdf.part", dir=dest_dir)
            os.close(fd)
            part_path = Path(part)
            try:
                self.engine.recognise(merged, part_path, self.cfg.ocr.languages, self.cancel_token)
                return _publish(part_path, dest_dir, job.stem)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    part_path.unlink()

    def _report(self, *, current: str = "", finished: Result | None = None, pages: int = 0) -> None:
        if self._progress_cb is None and finished is None:
            return
        with self._lock:
            if finished is not None:
                self._pages_done += pages
                self._docs_done += 1
            snapshot = Progress(
                self._pages_done, self._pages_total, self._docs_done, self._docs_total, current, finished
            )
            if self._progress_cb is not None:
                self._progress_cb(snapshot)


# --- steps -------------------------------------------------------------------


def document_name(cfg: Config, document: Document, n: int, pages: int, when: datetime) -> str:
    """Output file name (without extension) of ``document``. Raises ``naming.TemplateError``."""
    first = document.sources[0]
    values = naming.NameValues(
        when=when,
        dest=document.dest_name or document.dest_dir.expanduser().name,
        n=n,
        pages=pages,
        original=first.stem,
        scanner=document.scanner,
        scan_date=_scan_date(first),
    )
    return naming.render(document.template or cfg.naming.template, values)


def _is_pdf(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(5) == b"%PDF-"


def page_count(path: Path) -> int:
    """Pages a source will produce; errors are reported later, when it is processed."""
    try:
        if _is_pdf(path):
            with pikepdf.open(path) as pdf:
                return len(pdf.pages)
    except Exception:
        pass
    return 1


def _scan_date(path: Path) -> datetime | None:
    """EXIF capture date of a JPEG, else the file's modification time."""
    try:
        with Image.open(path) as im:
            raw = im.getexif().get_ifd(_EXIF_IFD).get(_EXIF_DATETIME_ORIGINAL)
        if raw:
            return datetime.strptime(raw.strip("\0 "), "%Y:%m:%d %H:%M:%S")
    except (OSError, ValueError, SyntaxError):
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _to_pdf(src: Path, dst: Path) -> Path:
    """Return a PDF for ``src``: the file itself if it is one, else a lossless conversion."""
    if _is_pdf(src):
        return src
    try:
        with Image.open(src) as im:
            fmt = im.format
            transparent = im.has_transparency_data
            if fmt in IMAGE_FORMATS and transparent:
                # A scan has no use for transparency, and OCR can see it as
                # black: flatten onto white (img2pdf would keep it as a mask).
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, "white")
                flat.paste(rgba, mask=rgba.getchannel("A"))
                image_path = dst.with_suffix(".png")
                flat.save(image_path, dpi=im.info.get("dpi", (300, 300)))
            else:
                image_path = src
    except OSError:
        fmt = None
    if fmt not in IMAGE_FORMATS:
        raise UnsupportedFile(_("{name}: unsupported file type (expected PNG, JPEG or PDF)").format(name=src.name))
    dst.write_bytes(img2pdf.convert(str(image_path), rotation=img2pdf.Rotation.ifvalid))
    return dst


def _merge(pdfs: list[Path], dst: Path) -> Path:
    with contextlib.ExitStack() as stack:
        out = stack.enter_context(pikepdf.new())
        for path in pdfs:
            src = stack.enter_context(pikepdf.open(path))
            out.pages.extend(src.pages)
        out.save(dst)
    return dst


def _publish(part: Path, dest_dir: Path, stem: str) -> Path:
    """Move ``part`` to ``<stem>.pdf``, or ``<stem>-1.pdf``, ``-2``… if taken."""
    suffix = 0
    while True:
        target = dest_dir / (f"{stem}.pdf" if suffix == 0 else f"{stem}-{suffix}.pdf")
        try:
            # Exclusive create reserves the name even against another worker.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            suffix += 1
            continue
        os.close(fd)
        os.replace(part, target)
        target.chmod(0o666 & ~_UMASK)  # mkstemp made the part file 0600
        return target


def _read_umask() -> int:
    # os.umask() can only be read by setting it: do it once, before any thread.
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


_UMASK = _read_umask()
