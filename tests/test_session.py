import time

import pikepdf
import pytest
from conftest import FakeEngine, SlowEngine
from gi.repository import GLib

from flypage.config import Config, Destination
from flypage.model import FAILED, IDLE, QUEUED
from flypage.importer import find_files
from flypage.session import Session


def wait_until(condition, timeout=15):
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not condition():
        if not context.iteration(False):
            time.sleep(0.01)
        if time.monotonic() > end:
            raise TimeoutError("condition not reached")


@pytest.fixture
def dest(tmp_path):
    return Destination(name="Invoices", path=str(tmp_path / "out"), icon="x-office-spreadsheet-symbolic")


@pytest.fixture
def make_session(samples):
    def make(n=4, engine=None, remove_sources=True):
        files = [samples.png(f"s{i}.png", f"Page {i}") for i in range(n)]
        cfg = Config()
        cfg.naming.template = "%{dest}_%n"
        cfg.ocr.languages = ["eng", "fra"]
        return Session(cfg, files, remove_sources=remove_sources, engine=engine or FakeEngine())

    return make


def names(session):
    return [i.name for i in session.items]


def test_find_files_skips_hidden_and_unsupported(tmp_path):
    (tmp_path / "DCIM" / "100MEDIA").mkdir(parents=True)
    (tmp_path / ".Trash-1000" / "files").mkdir(parents=True)
    for rel in ("DCIM/100MEDIA/IMAG0002.PDF", "DCIM/100MEDIA/IMAG0001.JPG", ".Trash-1000/files/old.JPG",
                "notes.txt", "a.png", "System Volume Information"):
        (tmp_path / rel).write_text("x")
    assert [p.relative_to(tmp_path).as_posix() for p in find_files(tmp_path)] == [
        "DCIM/100MEDIA/IMAG0001.JPG", "DCIM/100MEDIA/IMAG0002.PDF", "a.png",
    ]


def test_move_reorders(make_session):
    s = make_session(4)
    a, b, c, d = s.items
    s.move([a, b], before=None)
    assert names(s) == ["s2.png", "s3.png", "s0.png", "s1.png"]
    s.move([d], before=c)
    assert names(s) == ["s3.png", "s2.png", "s0.png", "s1.png"]


def test_each_file_is_its_own_document(make_session, dest):
    s = make_session(4)
    a, b, c, d = s.items
    finished = []
    s.connect("finished", lambda _s: finished.append(True))
    assert s.send([c, a], dest) == 2
    assert (a.state, c.state, b.state) == (QUEUED, QUEUED, IDLE)
    assert a.dest_icon == dest.icon
    wait_until(lambda: not s.busy)
    assert names(s) == ["s1.png", "s3.png"]
    # Numbered in grid order, one page each.
    assert {r.document.sources[0].name: r.output.name for r in s.results} == {
        "s0.png": "Invoices_1.pdf", "s2.png": "Invoices_2.pdf",
    }
    for r in s.results:
        with pikepdf.open(r.output) as pdf:
            assert len(pdf.pages) == 1
    s.send(s.items, dest)
    wait_until(lambda: finished)


def test_images_sent_as_they_are_when_conversion_is_off(make_session, dest, samples):
    engine = FakeEngine()
    s = make_session(1, engine=engine)
    pdf = samples.pdf("doc.pdf")
    s.store.append(type(s.items[0])(pdf, 1))
    png, doc = s.items
    s.convert_images = False
    assert s.passthrough(png) and not s.passthrough(doc)
    assert s.preview_name(png, dest) == "Invoices_1.png"
    s.send([png, doc], dest)
    wait_until(lambda: not s.busy)
    out = sorted(p.name for p in s.outputs())
    assert out == ["Invoices_1.png", "Invoices_2.pdf"]
    assert len(engine.calls) == 1  # only the PDF went through OCR
    assert not png.path.exists()  # moved


def test_copy_keeps_the_originals(make_session, dest):
    s = make_session(1)
    s.copy = True
    src = s.items[0].path
    s.send(s.items, dest)
    wait_until(lambda: not s.busy)
    assert src.exists() and len(s.outputs()) == 1


def test_languages_of_the_session_are_used(make_session, dest):
    engine = FakeEngine()
    s = make_session(1, engine=engine)
    assert s.languages == ["eng", "fra"]  # the configured ones by default
    s.languages = ["fra"]
    s.send(s.items, dest)
    wait_until(lambda: not s.busy)
    assert engine.calls[0][1] == ["fra"]


def test_originals_removed_only_when_asked(make_session, dest):
    s = make_session(1, remove_sources=False)
    src = s.items[0].path
    s.send(s.items, dest)
    wait_until(lambda: not s.busy)
    assert src.exists()

    s = make_session(1)
    src = s.items[0].path
    s.send(s.items, dest)
    wait_until(lambda: not s.busy)
    assert not src.exists()


def test_failure_keeps_the_file_with_its_error(samples, dest):
    bad = samples.root / "bad.jpg"
    bad.write_bytes(b"not an image")
    s = Session(Config(), [bad, samples.png()], engine=FakeEngine())
    s.send([s.items[0]], dest)
    wait_until(lambda: not s.busy)
    [item] = [i for i in s.items if i.name == "bad.jpg"]
    assert item.state == FAILED and "unsupported" in item.error
    assert bad.exists()
    # It can be sent again (e.g. after fixing): back to queued.
    s.send([item], dest)
    assert item.state == QUEUED
    wait_until(lambda: not s.busy)


def test_progress_signal_reaches_the_main_thread(make_session, dest):
    s = make_session(2)
    seen = []
    s.connect("progress", lambda _s, done, total, current: seen.append((done, total)))
    s.send(s.items, dest)
    wait_until(lambda: not s.busy)
    wait_until(lambda: (2, 2) in seen)


def test_queued_files_cannot_be_moved_or_deleted(make_session, dest):
    s = make_session(3, engine=SlowEngine())
    a, b, c = s.items
    s.send([a], dest)
    assert s.delete([a]) == [] and a in s.items and a.path.exists()
    s.move([a], before=None)
    assert s.items[0] is a
    s.cancel()
    wait_until(lambda: not s.busy)
    assert a.state == IDLE and a.path.exists()  # cancelled: back to the grid, nothing lost


def test_delete_removes_files_and_finishes_when_empty(make_session):
    s = make_session(2)
    finished = []
    s.connect("finished", lambda _s: finished.append(True))
    a, b = s.items
    assert s.delete([a]) == []
    assert not a.path.exists() and a not in s.items
    s.delete([b])
    assert finished == [True]


def test_ocr_error_is_raised_on_send(make_session, dest):
    s = make_session(1)
    s.languages = ["klingon"]
    from flypage.ocr import OcrError

    with pytest.raises(OcrError):
        s.send(s.items, dest)
    assert s.items[0].state == IDLE


def test_preview_name_follows_the_sequence(make_session, dest):
    s = make_session(3)
    a, b, c = s.items
    assert s.preview_name(a, dest) == "Invoices_1.pdf"
    s.send([a], dest)
    assert s.preview_name(b, dest) == "Invoices_2.pdf"
    wait_until(lambda: not s.busy)


def test_estimate_follows_the_queue(make_session, dest):
    s = make_session(2, engine=SlowEngine())
    assert s.estimate() is None
    a, b = s.items
    s.send([a, b], dest)
    wait_until(lambda: a.progress >= 0 or b.progress >= 0)
    snapshot = s.estimate()
    assert snapshot.pages_total == 2 and snapshot.pages_done == 0
    assert 0 <= snapshot.fraction < 1 and snapshot.seconds_left > 0
    s.cancel()
    wait_until(lambda: not s.busy)
    assert s.estimate() is None and a.progress == b.progress == -1


def test_speed_is_learned_from_real_ocr(make_session, dest):
    from flypage.progress import Speed

    s = make_session(1)
    s.send(s.items, dest)
    wait_until(lambda: not s.busy)
    assert Speed.load().samples == 1


def test_digital_pdf_needs_no_ocr(dest, samples):
    cfg = Config()
    cfg.naming.template = "%{dest}_%n"
    cfg.ocr.languages = ["klingon"]  # OCR could not even start
    s2 = Session(cfg, [samples.digital_pdf()], engine=FakeEngine())
    [item] = s2.items
    assert item.digital and s2.passthrough(item)
    assert s2.send([item], dest) == 1  # no OcrError: nothing to recognise
    wait_until(lambda: not s2.busy)
    assert [p.name for p in s2.outputs()] == ["Invoices_1.pdf"]
