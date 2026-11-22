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
    c.ocr.languages = ["eng", "fra"]
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


def test_image_passes_through_without_conversion(cfg, fake_engine, samples, out):
    jpg, pdf = samples.jpeg("IMAG0001.JPG"), samples.pdf()
    docs = [Document([jpg], out, convert_images=False), Document([pdf], out, convert_images=False)]
    with Pipeline(cfg, fake_engine, remove_sources=True) as pipeline:
        assert pipeline.output_name(docs[0]) == "Invoices_01.jpg"
        image, searchable = pipeline.run(docs)
    assert image.output.name == "Invoices_01.jpg" and searchable.output.name == "Invoices_02.pdf"
    assert len(fake_engine.calls) == 1  # a PDF always gets OCR
    assert not jpg.exists() and not pdf.exists()


def test_passthrough_refuses_what_is_not_an_image(cfg, fake_engine, samples, out):
    bad = samples.root / "bad.jpg"
    bad.write_bytes(b"not an image")
    [result] = run(cfg, fake_engine, [Document([bad], out, convert_images=False)])
    assert not result.ok and "unsupported" in result.error
    assert not out.exists() or list(out.iterdir()) == []


def test_document_languages_and_removal_override_the_pipeline(cfg, fake_engine, samples, out):
    png = samples.png()
    [result] = run(cfg, fake_engine, [Document([png], out, languages=["fra"], remove_sources=False)],
                   remove_sources=True)  # fmt: skip
    assert result.ok and png.exists()
    assert fake_engine.calls[0][1] == ["fra"]


def test_document_languages_are_checked(cfg, fake_engine, samples, out):
    with Pipeline(cfg, fake_engine) as pipeline, pytest.raises(OcrError):
        pipeline.submit(Document([samples.png()], out, languages=["klingon"]))


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


def test_ocr_options_reach_the_engine(cfg, fake_engine, samples, out):
    cfg.ocr.rotate_pages = True
    [result] = run(cfg, fake_engine, [Document([samples.png()], out, table_layout=True)])
    assert result.ok and result.ocr_seconds > 0
    assert fake_engine.options.rotate_pages and fake_engine.options.table_layout and not fake_engine.options.clean


def test_physical_cores_counts_cores_not_threads(tmp_path, monkeypatch):
    import flypage.pipeline as pipeline

    block = "processor\t: {n}\nphysical id\t: 0\ncore id\t\t: {core}\n\n"
    text = "".join(block.format(n=n, core=n % 2) for n in range(4))  # 2 cores, 4 threads
    real_read = pipeline.Path.read_text
    monkeypatch.setattr(pipeline.Path, "read_text",
                        lambda self, *a, **k: text if str(self) == "/proc/cpuinfo" else real_read(self, *a, **k))  # fmt: skip
    assert pipeline.physical_cores() == 2


def test_tesseract_command_line(tmp_path):
    from flypage.ocr import OcrOptions

    engine = TesseractEngine()
    cmd = engine.command(tmp_path / "a.pdf", tmp_path / "b.pdf", ["fra"], OcrOptions(table_layout=True))
    assert cmd[cmd.index("--tesseract-downsample-above") + 1] == "2600"
    assert cmd[cmd.index("--tesseract-pagesegmode") + 1] == "6"
    assert "--rotate-pages" not in cmd and "--clean" not in cmd
    assert cmd[-2:] == [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")]
    cmd = engine.command(tmp_path / "a.pdf", tmp_path / "b.pdf", ["fra"], OcrOptions(rotate_pages=True, clean=True))
    assert ("--rotate-pages" in cmd) == engine.can_rotate()
    assert ("--clean" in cmd) == engine.can_clean()


def test_tessdata_prefix_without_configs_is_reported(tmp_path, monkeypatch):
    engine = TesseractEngine()
    if not engine.available() or "eng" not in engine.languages():
        pytest.skip("Tesseract with eng not installed")
    monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path))
    with pytest.raises(OcrError, match="configs"):
        engine.check(["eng"])
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "hocr").write_text("tessedit_create_hocr 1\n")
    engine.check(["eng"])


def test_text_detection(samples):
    from flypage.pipeline import pdf_has_text

    assert pdf_has_text(samples.digital_pdf("a.pdf"))
    assert pdf_has_text(samples.digital_pdf("b.pdf", in_form=True))  # text layer of an OCR'd PDF
    assert not pdf_has_text(samples.pdf("scan.pdf"))  # image only: a scan
    assert not pdf_has_text(samples.png())  # not a PDF at all


def test_digital_pdf_is_filed_without_ocr(cfg, fake_engine, samples, out):
    digital, scan = samples.digital_pdf(), samples.pdf()
    docs = [Document([digital], out), Document([scan], out)]
    with Pipeline(cfg, fake_engine, remove_sources=True) as pipeline:
        assert pipeline.output_name(docs[0]) == "Invoices_01.pdf"
        filed, ocred = pipeline.run(docs)
    assert filed.ok and filed.ocr_seconds == 0 and filed.output.name == "Invoices_01.pdf"
    assert ocred.ok and ocred.ocr_seconds > 0
    assert len(fake_engine.calls) == 1  # only the scan
    assert not digital.exists()  # moved
