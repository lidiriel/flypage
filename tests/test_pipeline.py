import shutil
import subprocess
import threading
import time

import pikepdf
import pytest
from conftest import SlowEngine

from flypage.config import Config
from flypage.naming import TemplateError
from flypage.ocr import OcrError
from flypage.ocr.tesseract import TesseractEngine
from flypage.pipeline import Document, Pipeline


@pytest.fixture
def cfg():
    c = Config()
    c.naming.template = "%{dest}_%2n"
    return c


@pytest.fixture
def out(tmp_path):
    return tmp_path / "out" / "Invoices"


def pages_of(path):
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


def run(cfg, engine, documents, **kwargs):
    with Pipeline(cfg, engine, **kwargs) as pipeline:
        return pipeline.run(documents)


def test_png_jpeg_and_pdf_give_one_pdf_each(cfg, fake_engine, samples, out):
    sources = [samples.png(), samples.jpeg(), samples.pdf(pages=3)]
    results = run(cfg, fake_engine, [Document([s], out) for s in sources])

    assert [r.error for r in results] == [None, None, None]
    assert [r.output.name for r in results] == ["Invoices_01.pdf", "Invoices_02.pdf", "Invoices_03.pdf"]
    assert [pages_of(r.output) for r in results] == [1, 1, 3]
    assert all(s.exists() for s in sources)  # kept unless remove_sources
    assert {lang for _src, langs, _p in fake_engine.calls for lang in langs} == {"eng", "fra"}


def test_chain_merges_in_order(cfg, fake_engine, samples, out):
    a, b, c = samples.png("a.png", "A"), samples.pdf("b.pdf", pages=2), samples.jpeg("c.jpg", "C")
    [result] = run(cfg, fake_engine, [Document([c, a, b], out)])

    assert result.ok
    assert pages_of(result.output) == 4
    assert fake_engine.calls[0][2] == 4


def test_transparent_png_is_flattened(cfg, fake_engine, samples, out):
    [result] = run(cfg, fake_engine, [Document([samples.png(mode="RGBA")], out)])
    with pikepdf.open(result.output) as pdf:
        image = next(iter(pdf.pages[0].images.values()))
        assert "/SMask" not in image


def test_name_collisions_get_a_suffix(cfg, fake_engine, samples, out):
    out.mkdir(parents=True)
    (out / "Invoices_01.pdf").write_text("already here")
    [result] = run(cfg, fake_engine, [Document([samples.png()], out)])
    assert result.output.name == "Invoices_01-1.pdf"
    assert (out / "Invoices_01.pdf").read_text() == "already here"


def test_sequence_is_per_destination(cfg, fake_engine, samples, tmp_path):
    png = samples.png()
    docs = [Document([png], tmp_path / "A"), Document([png], tmp_path / "B"), Document([png], tmp_path / "A")]
    names = [r.output.name for r in run(cfg, fake_engine, docs)]
    assert names == ["A_01.pdf", "B_01.pdf", "A_02.pdf"]


def test_dest_name_and_template_override(cfg, fake_engine, samples, out):
    doc = Document([samples.png("IMG_7.png")], out, dest_name="Factures", template="%{original}-%{dest}-%{pages}")
    [result] = run(cfg, fake_engine, [doc])
    assert result.output.name == "IMG_7-Factures-1.pdf"


def test_output_name_preview(cfg, fake_engine, samples, out):
    with Pipeline(cfg, fake_engine) as pipeline:
        doc = Document([samples.pdf(pages=2)], out, template="%{dest}_%n_%{pages}p")
        assert pipeline.output_name(doc) == "Invoices_1_2p.pdf"
        assert pipeline.submit(doc).result().output.name == "Invoices_1_2p.pdf"
        assert pipeline.output_name(doc) == "Invoices_2_2p.pdf"


def test_bad_template_is_rejected_at_submit(cfg, fake_engine, samples, out):
    with Pipeline(cfg, fake_engine) as pipeline:
        with pytest.raises(TemplateError):
            pipeline.submit(Document([samples.png()], out, template="%{nope}"))
        # The failed submit did not use up a sequence number.
        assert pipeline.submit(Document([samples.png()], out)).result().output.name == "Invoices_01.pdf"


def test_one_failure_does_not_stop_the_others(cfg, fake_engine, samples, out):
    broken = samples.root / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 garbage")
    gif = samples.root / "anim.gif"
    from PIL import Image

    Image.new("RGB", (10, 10)).save(gif)
    good = samples.png()

    results = run(cfg, fake_engine, [Document([broken], out), Document([gif], out), Document([good], out)])

    assert [r.ok for r in results] == [False, False, True]
    assert "unsupported file type" in results[1].error
    assert results[0].error
    assert broken.exists() and gif.exists()
    assert not [p for p in out.iterdir() if p.name.startswith(".")]  # no leftover .part file


def test_remove_sources_only_after_success(cfg, fake_engine, samples, out):
    good, broken = samples.png(), samples.root / "broken.jpg"
    broken.write_bytes(b"not an image")
    results = run(cfg, fake_engine, [Document([good], out), Document([broken], out)], remove_sources=True)
    assert [r.ok for r in results] == [True, False]
    assert not good.exists()
    assert broken.exists()


def test_progress_reports_pages(cfg, fake_engine, samples, out):
    events = []
    docs = [Document([samples.pdf("a.pdf", pages=2)], out), Document([samples.png()], out)]
    run(cfg, fake_engine, docs, progress=events.append, workers=1)

    finished = [e for e in events if e.finished]
    assert [(e.pages_done, e.pages_total, e.documents_done) for e in finished] == [(2, 3, 1), (3, 3, 2)]
    assert [e.current for e in events if not e.finished] == ["Invoices_01.pdf", "Invoices_02.pdf"]


def test_work_files_are_cleaned_up(cfg, fake_engine, samples, out, tmp_path):
    run(cfg, fake_engine, [Document([samples.png(), samples.jpeg()], out)])
    assert list((tmp_path / "cache" / "flypage" / "work").iterdir()) == []


def test_cancel_kills_running_ocr_and_skips_queued(cfg, samples, out):
    events = []
    started = threading.Event()

    def progress(p):
        events.append(p)
        if p.current:
            started.set()

    png = samples.png()
    pipeline = Pipeline(cfg, SlowEngine(), workers=1, progress=progress)
    futures = [pipeline.submit(Document([png], out)) for _ in range(3)]
    assert started.wait(10)
    time.sleep(0.3)  # let the first document reach "sleep 30"
    t0 = time.monotonic()
    pipeline.cancel()
    results = [f.result(timeout=10) for f in futures]
    pipeline.close()

    assert time.monotonic() - t0 < 5
    assert all(r.cancelled and not r.ok for r in results), [(r.cancelled, r.error) for r in results]
    assert not out.exists() or list(out.iterdir()) == []
    assert subprocess.run(["pgrep", "-f", "^sleep 30$"], capture_output=True).returncode != 0


def test_missing_ocr_language_fails_early(cfg, fake_engine):
    cfg.ocr.languages = ["eng", "klingon"]
    with pytest.raises(OcrError, match="klingon"):
        Pipeline(cfg, fake_engine)


def test_easyocr_not_supported_yet(cfg):
    cfg.ocr.engine = "easyocr"
    with pytest.raises(OcrError):
        Pipeline(cfg)


tesseract = pytest.mark.skipif(not TesseractEngine().available(), reason="ocrmypdf/tesseract not installed")


@tesseract
def test_tesseract_adds_a_text_layer(cfg, samples, out):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    png = samples.png(text="Invoice Flypage")
    pdf = samples.pdf(text="Bonjour", pages=2)
    [single, chain] = run(cfg, None, [Document([png], out), Document([png, pdf], out)])

    def text(path):
        return subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, check=True).stdout

    assert single.ok, single.error
    assert "Invoice Flypage" in text(single.output)
    assert chain.ok, chain.error
    assert pages_of(chain.output) == 3
    chain_text = text(chain.output)
    assert "Invoice Flypage" in chain_text and "Bonjour 2" in chain_text


@tesseract
def test_tesseract_languages_are_listed():
    assert "eng" in TesseractEngine().languages()
    assert "osd" not in TesseractEngine().languages()


@tesseract
def test_scanner_jpeg_quirk_is_accepted(cfg, samples, out):
    """ocrmypdf exits with "invalid output" on such JPEGs; the PDF is still kept."""
    if not shutil.which("qpdf"):
        pytest.skip("qpdf not installed")
    jpeg = samples.scanner_jpeg(text="Hello Flypage")
    as_pdf = samples.root / "quirk.pdf"
    import img2pdf

    as_pdf.write_bytes(img2pdf.convert(str(jpeg)))
    check = subprocess.run(["qpdf", "--check", str(as_pdf)], capture_output=True, text=True)
    assert "corrupt" in check.stdout + check.stderr  # the fixture reproduces the quirk

    [from_jpeg, from_pdf] = run(cfg, None, [Document([jpeg], out), Document([as_pdf], out)])
    assert from_jpeg.ok, from_jpeg.error
    assert from_pdf.ok, from_pdf.error
    assert pages_of(from_jpeg.output) == 1


def test_invalid_output_with_wrong_pages_is_still_an_error(tmp_path, samples, monkeypatch):
    from flypage.cancel import CancelToken
    from flypage.ocr import tesseract as t

    src = samples.pdf(pages=2)
    dst = tmp_path / "dst.pdf"

    def fake_run(cmd, **kwargs):
        shutil.copyfile(samples.pdf("one.pdf", pages=1), dst)  # wrong page count
        return subprocess.CompletedProcess(cmd, t.EXIT_INVALID_OUTPUT, "", "Output file: The generated PDF is INVALID")

    token = CancelToken()
    monkeypatch.setattr(token, "run", fake_run)
    with pytest.raises(OcrError, match="exit code 4"):
        t.TesseractEngine().recognise(src, dst, ["eng"], token)
