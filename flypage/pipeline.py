"""Turn scanned files into searchable PDFs (PLAN.md §7).

For each ``Document`` (one file, or several files merged into one PDF):
normalise every file to PDF, merge, OCR, write ``<dest>/<name>.pdf``, and
optionally remove the originals. Two kinds of single-file documents are
written as they are, without OCR, under the same kind of name: an image
when ``convert_images`` is off (``<dest>/<name>.jpg``), and a *digital* PDF,
one that already has text (``<dest>/<name>.pdf``). A failing document does
not stop the others.

Documents are queued with ``Pipeline.submit()`` while others are running, which
is what the processing window does on each drag-and-drop. ``run()`` is the
batch shortcut used by the command line.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import shutil
import tempfile
import threading
import time
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
from flypage.ocr import OcrEngine, OcrOptions, get_engine

log = logging.getLogger(__name__)

IMAGE_FORMATS = ("PNG", "JPEG")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
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
    languages: list[str] | None = None  # OCR languages; None = the configured ones
    convert_images: bool = True  # False: a single image is written as it is, without OCR
    remove_sources: bool | None = None  # None = the pipeline's setting
    table_layout: bool = False  # OCR reads tables row by row

    @functools.cached_property
    def passthrough(self) -> bool:
        """True if this document is written as it is, without OCR.

        That is a single image with ``convert_images`` off, or a single PDF
        that already has text (a digital PDF: OCR would add nothing).
        """
        if len(self.sources) != 1:
            return False
        src = self.sources[0]
        if _is_pdf_quiet(src):
            return pdf_has_text(src)
        return not self.convert_images

    def output_suffix(self) -> str:
        if self.passthrough and not _is_pdf_quiet(self.sources[0]):
            suffix = self.sources[0].suffix.lower()
            return suffix if suffix in IMAGE_SUFFIXES else ".jpg"
        return ".pdf"


@dataclass
class Result:
    document: Document
    output: Path | None = None
    error: str | None = None
    cancelled: bool = False
    ocr_seconds: float = 0.0  # time spent in OCR (0 for an image written as it is)

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
    started: Document | None = None  # set when a document has just started


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
        check: bool = True,
    ) -> None:
        """``check=False``: do not fail now if the configured languages are missing
        (the caller checks the languages of the documents that need OCR)."""
        self.cfg = cfg
        self.engine = engine or get_engine(cfg.ocr.engine)
        if check:
            self.engine.check(cfg.ocr.languages)
        self.remove_sources = remove_sources
        self.started = datetime.now()
        self.cancel_token = CancelToken()
        self._progress_cb = progress
        self._lock = threading.Lock()
        self._sequence: dict[Path, int] = {}
        self._pages_done = self._pages_total = 0
        self._docs_done = self._docs_total = 0
        self.workers = min(filter(None, (workers, self.engine.max_workers, physical_cores())))
        self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="flypage-ocr")

    # --- public API ----------------------------------------------------------

    def submit(self, document: Document) -> Future[Result]:
        """Queue ``document``.

        Raises ``naming.TemplateError`` if its name cannot be built, ``OcrError``
        if its languages are not installed.
        """
        if not document.sources:
            raise ValueError("a document needs at least one source file")
        if document.languages is not None and not document.passthrough:
            self.engine.check(document.languages)
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
        return self._name(document, n, pages) + document.output_suffix()

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
            self._report(current=job.stem + job.document.output_suffix(), started=job.document)
            if job.document.passthrough:
                result.output = self._pass_through(job)
            else:
                result.output = self._convert(job, result)
        except Cancelled:
            result.cancelled = True
            result.error = _("Cancelled")
        except Exception as err:  # one bad document must not stop the others
            log.debug("processing %s failed", job.document.sources, exc_info=True)
            result.error = str(err) or type(err).__name__
        else:
            remove = job.document.remove_sources
            if remove if remove is not None else self.remove_sources:
                for src in job.document.sources:
                    try:
                        src.unlink()
                    except OSError as err:
                        log.warning("cannot remove %s: %s", src, err)
        self._report(finished=result, pages=job.pages)
        return result

    def _pass_through(self, job: _Job) -> Path:
        """Copy an image or a digital PDF to the destination as it is (no conversion, no OCR)."""
        src = job.document.sources[0]
        if not _is_pdf_quiet(src) and _image_format(src) not in IMAGE_FORMATS:
            raise UnsupportedFile(_("{name}: unsupported file type (expected PNG, JPEG or PDF)").format(name=src.name))
        dest_dir = job.document.dest_dir.expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = job.document.output_suffix()
        fd, part = tempfile.mkstemp(prefix=".flypage-", suffix=suffix + ".part", dir=dest_dir)
        os.close(fd)
        part_path = Path(part)
        try:
            shutil.copyfile(src, part_path)
            self.cancel_token.check()
            return _publish(part_path, dest_dir, job.stem, suffix)
        finally:
            with contextlib.suppress(FileNotFoundError):
                part_path.unlink()

    def _convert(self, job: _Job, result: Result) -> Path:
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
                languages = job.document.languages or self.cfg.ocr.languages
                options = OcrOptions(
                    rotate_pages=self.cfg.ocr.rotate_pages,
                    clean=self.cfg.ocr.clean,
                    table_layout=job.document.table_layout,
                )
                start = time.monotonic()
                self.engine.recognise(merged, part_path, languages, self.cancel_token, options)
                result.ocr_seconds = time.monotonic() - start
                return _publish(part_path, dest_dir, job.stem)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    part_path.unlink()

    def _report(
        self, *, current: str = "", finished: Result | None = None, pages: int = 0, started: Document | None = None
    ) -> None:
        if self._progress_cb is None and finished is None:
            return
        with self._lock:
            if finished is not None:
                self._pages_done += pages
                self._docs_done += 1
            snapshot = Progress(
                self._pages_done, self._pages_total, self._docs_done, self._docs_total, current, finished, started
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


def physical_cores() -> int:
    """CPU cores, not hardware threads: two Tesseracts on one core's threads compete (PLAN.md §7.1)."""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    cores = set()
    physical = core = None
    for line in text.splitlines() + [""]:
        key, _sep, value = line.partition(":")
        key = key.strip()
        if key == "physical id":
            physical = value.strip()
        elif key == "core id":
            core = value.strip()
        elif not key:  # blank line: end of one processor's block
            if core is not None:
                cores.add((physical, core))
            physical = core = None
    return len(cores) or os.cpu_count() or 1


_TEXT_OPERATORS = frozenset({"Tj", "TJ", "'", '"'})


def pdf_has_text(path: Path) -> bool:
    """True if the PDF draws text anywhere (visible or not): it is digital, or already OCR'd."""
    try:
        with pikepdf.open(path) as pdf:
            return any(_draws_text(page.obj, set()) for page in pdf.pages)
    except Exception:  # unreadable: let OCR report the problem
        return False


def _draws_text(obj, seen: set) -> bool:
    for _operands, operator in pikepdf.parse_content_stream(obj):
        if str(operator) in _TEXT_OPERATORS:
            return True
    # Text can also be in form XObjects (ocrmypdf puts its text layer in one).
    resources = obj.get("/Resources")
    xobjects = resources.get("/XObject") if resources is not None else None
    if xobjects is None:
        return False
    for name in xobjects.keys():
        xobject = xobjects[name]
        if xobject.objgen in seen or xobject.get("/Subtype") != "/Form":
            continue
        seen.add(xobject.objgen)
        if _draws_text(xobject, seen):
            return True
    return False


def _is_pdf_quiet(path: Path) -> bool:
    try:
        return _is_pdf(path)
    except OSError:
        return False


def _image_format(path: Path) -> str | None:
    try:
        with Image.open(path) as im:
            return im.format
    except OSError:
        return None


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


def _publish(part: Path, dest_dir: Path, stem: str, ext: str = ".pdf") -> Path:
    """Move ``part`` to ``<stem><ext>``, or ``<stem>-1<ext>``, ``-2``… if taken."""
    suffix = 0
    while True:
        target = dest_dir / (f"{stem}{ext}" if suffix == 0 else f"{stem}-{suffix}{ext}")
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
