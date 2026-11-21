"""OCR engines and their registry."""

from flypage.i18n import _
from flypage.ocr.base import OcrEngine, OcrError
from flypage.ocr.tesseract import TesseractEngine

__all__ = ["OcrEngine", "OcrError", "get_engine"]


def get_engine(name: str) -> OcrEngine:
    if name == "tesseract":
        return TesseractEngine()
    raise OcrError(_("Unknown OCR engine “{name}”.").format(name=name))
