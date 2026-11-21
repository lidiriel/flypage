import time

import pikepdf
import pytest
from conftest import FakeEngine, SlowEngine
from gi.repository import GLib

from flypage.config import Config, Destination
from flypage.model import FAILED, IDLE, QUEUED
from flypage.importer import find_files
from flypage.session import ALL_SAME, PER_FILE, Session


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
    def make(n=4, mode=PER_FILE, engine=None, remove_sources=True):
        files = [samples.png(f"s{i}.png", f"Page {i}") for i in range(n)]
        cfg = Config()
        cfg.naming.template = "%{dest}_%n"
        return Session(cfg, files, remove_sources=remove_sources, engine=engine or FakeEngine(), sort_mode=mode)

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


def test_chain_gathers_files_and_toggles(make_session):
    s = make_session(5)
    a, b, c, d, e = s.items
    s.toggle_chain([b, d])
    assert b.chain and b.chain == d.chain
    assert names(s) == ["s0.png", "s1.png", "s3.png", "s2.png", "s4.png"]  # d moved next to b

    s.toggle_chain([d, b])  # exactly that chain again: unchain
    assert b.chain == d.chain == 0


def test_chaining_part_of_a_chain_dissolves_leftovers(make_session):
    s = make_session(4)
    a, b, c, d = s.items
    s.toggle_chain([a, b])
    s.toggle_chain([b, c, d])
    assert a.chain == 0  # alone in its old chain
    assert b.chain == c.chain == d.chain != 0


def test_single_file_cannot_be_chained(make_session):
    s = make_session(2)
    s.toggle_chain([s.items[0]])
    assert s.items[0].chain == 0


def test_remove_from_chain(make_session):
    s = make_session(3)
    a, b, c = s.items
    s.toggle_chain([a, b, c])
    s.remove_from_chain(b)
    assert (a.chain, b.chain) == (c.chain, 0)
    s.remove_from_chain(a)
    assert c.chain == 0  # a chain of one is no chain


def test_move_keeps_chains_together(make_session):
    s = make_session(4)
    a, b, c, d = s.items
    s.toggle_chain([a, b])
    s.move([a], before=None)  # dragging one chain member drags the chain
    assert names(s) == ["s2.png", "s3.png", "s0.png", "s1.png"]
    s.move([d], before=c)
    assert names(s) == ["s3.png", "s2.png", "s0.png", "s1.png"]


def test_per_file_sends_only_dragged_files_and_their_chain(make_session, dest):
    s = make_session(4)
    a, b, c, d = s.items
    s.toggle_chain([b, c])
    assert s.send([b], dest) == 1  # one document: the chain
    assert (b.state, c.state, a.state) == (QUEUED, QUEUED, IDLE)
    assert b.dest_icon == dest.icon
    wait_until(lambda: not s.busy)
    assert names(s) == ["s0.png", "s3.png"]
    [result] = s.results
    with pikepdf.open(result.output) as pdf:
        assert len(pdf.pages) == 2
    assert result.output.name == "Invoices_1.pdf"


def test_all_same_sends_everything(make_session, dest):
    s = make_session(3, mode=ALL_SAME)
    finished = []
    s.connect("finished", lambda _s: finished.append(True))
    assert s.send([s.items[0]], dest) == 3
    wait_until(lambda: finished)
    assert s.items == []
    assert sorted(p.name for p in s.outputs()) == ["Invoices_1.pdf", "Invoices_2.pdf", "Invoices_3.pdf"]


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


def test_queued_files_cannot_be_chained_moved_or_deleted(make_session, dest):
    s = make_session(3, engine=SlowEngine())
    a, b, c = s.items
    s.send([a], dest)
    s.toggle_chain([a, b])
    assert a.chain == 0
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
    s.toggle_chain([a, b])
    assert s.delete([a]) == []
    assert not a.path.exists() and b.chain == 0
    s.delete([b])
    assert finished == [True]


def test_ocr_error_is_raised_on_send(make_session, dest):
    s = make_session(1)
    s.cfg.ocr.languages = ["klingon"]
    from flypage.ocr import OcrError

    with pytest.raises(OcrError):
        s.send(s.items, dest)
    assert s.items[0].state == IDLE


def test_preview_name_follows_the_sequence(make_session, dest):
    s = make_session(3)
    a, b, c = s.items
    assert s.preview_name([a], dest) == "Invoices_1.pdf"
    assert s.preview_name([a, b], dest) is None  # two documents: no single name
    s.toggle_chain([b, c])
    assert s.preview_name([b], dest) == "Invoices_1.pdf"  # the chain is one document
    s.send([a], dest)
    assert s.preview_name([b], dest) == "Invoices_2.pdf"
    wait_until(lambda: not s.busy)
