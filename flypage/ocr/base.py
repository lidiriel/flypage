"""Interface every OCR engine implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from flypage.cancel import CancelToken
from flypage.i18n import _


class OcrError(Exception):
    """OCR could not run or failed; the message is shown to the user."""


@dataclass(frozen=True)
class OcrOptions:
    """Optional OCR steps (PLAN.md §7.1); all off by default."""

    rotate_pages: bool = False  # turn pages scanned upside down or sideways
    clean: bool = False  # clean the image sent to OCR (unpaper); the output image is unchanged
    table_layout: bool = False  # read tables row by row (worse on text in columns)


class OcrEngine(ABC):
    name: str
    label: str
    #: Upper bound on documents processed in parallel (None = one per CPU).
    max_workers: int | None = None

    @abstractmethod
    def available(self) -> bool:
        """True if the engine's software is installed."""

    @abstractmethod
    def languages(self) -> list[str]:
        """Language codes this engine can recognise on this computer."""

    @abstractmethod
    def recognise(
        self, src: Path, dst: Path, languages: list[str], cancel: CancelToken, options: OcrOptions | None = None
    ) -> None:
        """Write ``dst``: the pages of PDF ``src`` with an invisible text layer added."""

    def check(self, languages: list[str]) -> None:
        """Raise ``OcrError`` if the engine cannot run with ``languages``."""
        if not self.available():
            raise OcrError(_("{engine} is not installed.").format(engine=self.label))
        missing = [lang for lang in languages if lang not in self.languages()]
        if missing:
            raise OcrError(
                _("OCR languages not installed for {engine}: {languages}").format(
                    engine=self.label, languages=", ".join(missing)
                )
            )
