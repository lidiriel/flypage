"""Tesseract, driven through ocrmypdf."""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pikepdf

from flypage.cancel import CancelToken
from flypage.i18n import _
from flypage.ocr.base import OcrEngine, OcrError, OcrOptions

log = logging.getLogger(__name__)

# Tesseract only sees a copy of the page reduced to this size (the PDF keeps
# the full image): ≈30 % faster on IRIScan pages, same accuracy (PLAN.md §7.1).
DOWNSAMPLE_ABOVE = 2600
TABLE_PAGESEGMODE = "6"  # one uniform block of text: tables are read row by row


class TesseractEngine(OcrEngine):
    name = "tesseract"
    label = "Tesseract"

    def available(self) -> bool:
        return shutil.which("ocrmypdf") is not None and shutil.which("tesseract") is not None

    @functools.cache
    def _models(self) -> tuple[str, ...]:
        """Every installed model, ``osd`` (orientation detection) included."""
        try:
            out = subprocess.run(
                ["tesseract", "--list-langs"], capture_output=True, text=True, check=True
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return ()
        # First line is a header ("List of available languages in ...").
        return tuple(line.strip() for line in out.splitlines()[1:] if line.strip())

    def languages(self) -> list[str]:
        return [m for m in self._models() if m != "osd"]

    def can_rotate(self) -> bool:
        return "osd" in self._models()

    def check(self, languages: list[str]) -> None:
        super().check(languages)
        # A custom TESSDATA_PREFIX needs Tesseract's "configs" folder too, or
        # Tesseract cannot write the hOCR that ocrmypdf turns into a text layer.
        prefix = os.environ.get("TESSDATA_PREFIX")
        if prefix and not (Path(prefix) / "configs" / "hocr").is_file():
            raise OcrError(
                _(
                    "Tesseract's “configs” folder is missing from TESSDATA_PREFIX ({prefix}). "
                    "Link it there: ln -s /usr/share/tesseract-ocr/5/tessdata/configs {prefix}/"
                ).format(prefix=prefix)
            )

    @staticmethod
    def can_clean() -> bool:
        return shutil.which("unpaper") is not None

    def command(self, src: Path, dst: Path, languages: list[str], options: OcrOptions) -> list[str]:
        cmd = [
            "ocrmypdf",
            "--skip-text",  # keep pages that already have text as they are
            "--output-type", "pdf",
            "--jobs", "1",  # the pipeline already runs one document per core
            "--quiet",
            "-l", "+".join(languages),
            "--tesseract-downsample-above", str(DOWNSAMPLE_ABOVE),
        ]  # fmt: skip
        if options.rotate_pages:
            if self.can_rotate():
                cmd.append("--rotate-pages")
            else:
                log.warning("rotate pages: the Tesseract model “osd” is not installed, skipped")
        if options.clean:
            if self.can_clean():
                cmd.append("--clean")
            else:
                log.warning("clean pages: unpaper is not installed, skipped")
        if options.table_layout:
            cmd += ["--tesseract-pagesegmode", TABLE_PAGESEGMODE]
        return cmd + [str(src), str(dst)]

    def recognise(
        self, src: Path, dst: Path, languages: list[str], cancel: CancelToken, options: OcrOptions | None = None
    ) -> None:
        cmd = self.command(src, dst, languages, options or OcrOptions())
        # One thread per Tesseract: the pipeline runs one document per core already.
        env = dict(os.environ, OMP_THREAD_LIMIT="1")
        result = cancel.run(cmd, env=env)
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
