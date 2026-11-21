import io
import shutil
from pathlib import Path

import img2pdf
import pikepdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from flypage.cancel import CancelToken
from flypage.ocr.base import OcrEngine


@pytest.fixture(autouse=True)
def isolate_xdg(tmp_path, monkeypatch):
    """Never touch the real ~/.config or ~/.cache."""
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        monkeypatch.setenv(var, str(tmp_path / sub))


def _image(text: str, mode: str = "RGB") -> Image.Image:
    im = Image.new(mode, (1240, 500), "white")
    ImageDraw.Draw(im).text((80, 200), text, fill="black", font=ImageFont.load_default(size=60))
    return im


@pytest.fixture
def samples(tmp_path):
    """Factory for sample scans: samples.png("a.png", "text"), .jpeg(...), .pdf(..., pages=2)."""

    class Samples:
        root = tmp_path / "in"

        def __init__(self):
            self.root.mkdir()

        def png(self, name="scan.png", text="Hello", mode="RGB"):
            path = self.root / name
            _image(text, mode).save(path, dpi=(150, 150))
            return path

        def jpeg(self, name="scan.jpg", text="Hello"):
            path = self.root / name
            _image(text).save(path, dpi=(150, 150))
            return path

        def scanner_jpeg(self, name="IMAG0001.JPG", text="Hello"):
            """A JPEG like the IRIScan Book writes: Pillow decodes it, libjpeg/qpdf warn "corrupt"."""
            buf = io.BytesIO()
            _image(text).save(buf, "JPEG", dpi=(150, 150))
            data = bytearray(buf.getvalue())
            sos = data.index(b"\xff\xda")
            start = sos + 2 + int.from_bytes(data[sos + 2 : sos + 4], "big")
            i = start + int((len(data) - start) * 0.9)
            while data[i] in (0xFF, 0x00) or data[i - 1] == 0xFF:
                i += 1
            data[i] ^= 0x55
            path = self.root / name
            path.write_bytes(bytes(data))
            return path

        def pdf(self, name="scan.pdf", pages=1, text="Hello"):
            path = self.root / name
            pngs = [self.png(f".{name}-{i}.png", f"{text} {i + 1}") for i in range(pages)]
            path.write_bytes(img2pdf.convert([str(p) for p in pngs]))
            for png in pngs:
                png.unlink()
            return path

    return Samples()


class FakeEngine(OcrEngine):
    """Copies the PDF instead of running OCR, and records what it was given."""

    name = "fake"
    label = "Fake"

    def __init__(self):
        self.calls: list[tuple[Path, list[str], int]] = []  # (src, languages, pages)

    def available(self):
        return True

    def languages(self):
        return ["eng", "fra"]

    def recognise(self, src, dst, languages, cancel: CancelToken):
        cancel.check()
        with pikepdf.open(src) as pdf:
            pages = len(pdf.pages)
        self.calls.append((src, list(languages), pages))
        shutil.copyfile(src, dst)


class SlowEngine(FakeEngine):
    """Blocks in an external command until cancelled."""

    def recognise(self, src, dst, languages, cancel: CancelToken):
        cancel.run(["sleep", "30"])
        shutil.copyfile(src, dst)


@pytest.fixture
def fake_engine():
    return FakeEngine()
