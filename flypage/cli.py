"""Commands that run without a window."""

from __future__ import annotations

import signal
import sys
from argparse import Namespace
from pathlib import Path

from flypage.config import Config, Destination
from flypage.i18n import _

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130


def _error(message: str) -> None:
    print(f"flypage: {message}", file=sys.stderr)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def _configured_destination(cfg: Config, dest_dir: Path) -> Destination | None:
    target = dest_dir.resolve()
    return next((d for d in cfg.destinations if d.expanded_path().resolve() == target), None)


def process(args: Namespace, cfg: Config) -> int:
    # The PDF and image libraries are only loaded when actually needed.
    from flypage.naming import TemplateError
    from flypage.ocr import OcrError
    from flypage.pipeline import Document, Pipeline, Progress

    files = [Path(f) for f in args.files]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        _error(_("file not found: {files}").format(files=", ".join(missing)))
        return EXIT_USAGE

    # A folder that is one of the configured destinations uses its name for
    # %{dest} and its own file name template, as in the processing window.
    dest_dir = Path(args.dest).expanduser()
    dest = _configured_destination(cfg, dest_dir)
    common = {"dest_dir": dest_dir, "dest_name": dest.name if dest else "", "template": dest.template if dest else ""}
    documents = [Document(files, **common)] if args.merge else [Document([f], **common) for f in files]

    def show(progress: Progress) -> None:
        result = progress.finished
        if result is None:
            return
        if result.ok:
            print(result.output, flush=True)
        elif not result.cancelled:
            _error(f"{result.document.sources[0].name}: {result.error}")
        if sys.stderr.isatty():
            print(
                _("OCR {done} / {total} pages").format(done=progress.pages_done, total=progress.pages_total),
                file=sys.stderr,
            )

    try:
        pipeline = Pipeline(cfg, progress=show)
    except OcrError as err:
        _error(str(err))
        return EXIT_FAILED

    # OCR runs in its own process groups, so being killed must go through
    # cancel() too, or those processes would be left running.
    signal.signal(signal.SIGTERM, _raise_interrupt)

    with pipeline:
        try:
            futures = [pipeline.submit(d) for d in documents]
        except TemplateError as err:
            _error(str(err))
            pipeline.cancel()
            return EXIT_USAGE
        try:
            results = [f.result() for f in futures]
        except KeyboardInterrupt:
            _error(_("cancelling…"))
            pipeline.cancel()
            return EXIT_INTERRUPTED

    return EXIT_OK if all(r.ok for r in results) else EXIT_FAILED


def import_window(args: Namespace) -> int:
    """Processing window on the files the daemon copied from a scanner."""
    from flypage.importer import ScannerFiles

    staging = Path(args.staging)
    try:
        originals = ScannerFiles(staging)
    except (OSError, ValueError, KeyError, TypeError) as err:
        _error(_("not an import folder: {folder} ({error})").format(folder=staging, error=err))
        return EXIT_USAGE

    from flypage.application import run_processing

    return run_processing(originals.files(), originals.scanner, remove_sources=True, originals=originals)


def sort(args: Namespace) -> int:
    from flypage.importer import find_files

    folder = Path(args.directory).expanduser()
    if not folder.is_dir():
        _error(_("not a folder: {folder}").format(folder=folder))
        return EXIT_USAGE
    files = find_files(folder)
    if not files:
        _error(_("no PNG, JPEG or PDF file in {folder}").format(folder=folder))
        return EXIT_FAILED

    from flypage.application import run_processing

    return run_processing(files, folder.name or str(folder), remove_sources=not args.keep_originals)
