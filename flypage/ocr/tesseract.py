"""Tesseract, driven through ocrmypdf."""

from __future__ import annotations

import functools
import logging
import shutil
import subprocess
from pathlib import Path

import pikepdf

from flypage.cancel import CancelToken
from flypage.i18n import _
from flypage.ocr.base import OcrEngine, OcrError

log = logging.getLogger(__name__)


class TesseractEngine(OcrEngine):
    name = "tesseract"
    label = "Tesseract"

    def available(self) -> bool:
        return shutil.which("ocrmypdf") is not None and shutil.which("tesseract") is not None

    @functools.cache
    def languages(self) -> list[str]:
        try:
            out = subprocess.run(
                ["tesseract", "--list-langs"], capture_output=True, text=True, check=True
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return []
        # First line is a header ("List of available languages in ...").
        return [line.strip() for line in out.splitlines()[1:] if line.strip() and line.strip() != "osd"]

    def recognise(self, src: Path, dst: Path, languages: list[str], cancel: CancelToken) -> None:
        cmd = [
            "ocrmypdf",
            "--skip-text",  # keep pages that already have text as they are
            "--output-type", "pdf",
            "--jobs", "1",  # the pipeline already runs one document per CPU
            "--quiet",
            "-l", "+".join(languages),
            str(src),
            str(dst),
        ]  # fmt: skip
        result = cancel.run(cmd)
        if result.returncode == EXIT_INVALID_OUTPUT and _same_pages(src, dst):
            # Some scanners (e.g. IRIScan Book) write JPEG data that libjpeg
            # decodes with a warning. ocrmypdf passes the image through
            # unchanged, then its strict final check (qpdf) rejects the output.
            # The PDF is as good as the scan itself: keep it.
            log.info("%s: ocrmypdf flagged the output as invalid (scanner JPEG data), kept", src.name)
            return
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or [""])[-1]
            raise OcrError(
                _("ocrmypdf failed (exit code {code}): {detail}").format(code=result.returncode, detail=detail)
            )


EXIT_INVALID_OUTPUT = 4  # ocrmypdf.ExitCode.invalid_output_pdf


def _same_pages(src: Path, dst: Path) -> bool:
    """True if ``dst`` is a readable PDF with as many pages as ``src``."""
    try:
        with pikepdf.open(src) as a, pikepdf.open(dst) as b:
            return len(b.pages) == len(a.pages) > 0
    except Exception:
        return False
