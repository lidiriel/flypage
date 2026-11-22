"""Processing window behaviour. Widgets are built but not shown; needs a display."""

import pytest

import flypage.ui  # noqa: F401
from gi.repository import Adw, Gdk, Gtk

# init_check() can return True with no display at all: also check there is one.
if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from conftest import FakeEngine  # noqa: E402
from test_session import wait_until  # noqa: E402

from flypage import config  # noqa: E402
from flypage.config import Config, Destination  # noqa: E402
from flypage.model import QUEUED  # noqa: E402
from flypage.session import Session  # noqa: E402
from flypage.ui.thumbnail_grid import drag_payload, parse_payload  # noqa: E402
from flypage.ui.processing_window import ProcessingWindow  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    c = Config(
        destinations=[
            Destination(path=str(tmp_path / "Documents")),
            Destination(path=str(tmp_path / "Invoices")),
        ]
    )
    c.naming.template = "%{dest}_%n"
    c.ocr.languages = ["eng", "fra"]
    config.save(c)
    return c


@pytest.fixture
def window(cfg, samples):
    files = [samples.png(f"s{i}.png", f"Page {i}") for i in range(4)]
    session = Session(cfg, files, engine=FakeEngine())
    win = ProcessingWindow(session, "Test")
    yield win
    session.cancel()
    win.destroy()


def select(win, *positions):
    win.grid.selection.unselect_all()
    for p in positions:
        win.grid.selection.select_item(p, False)


def test_payload_round_trip(window):
    items = window.session.items[:2]
    assert parse_payload(drag_payload(items)) == [i.id for i in items]
    assert parse_payload("some text from another app") is None
    assert parse_payload(42) is None


def test_message_and_delete_follow_the_selection(window):
    assert "4 files from Test" in window.message.get_label()
    assert not window.delete_action.get_enabled()
    select(window, 0)
    assert window.delete_action.get_enabled()
    select(window, 0, 2)
    assert window.preview.get_visible_child_name() == "multi"


def test_defaults_of_the_toolbar(window):
    assert window.convert.get_active() and window.session.convert_images
    assert window.transfer.get_active_name() == "move" and not window.session.copy
    assert window.language.get_selected() == 0 and window.session.languages == ["eng"]
    assert window.language_choices == [["eng"], ["fra"], ["eng", "fra"]]  # last: all languages


def test_toolbar_changes_the_session(window):
    window.transfer.set_active_name("copy")
    assert window.session.copy
    window.language.set_selected(2)
    assert window.session.languages == ["eng", "fra"]
    select(window, 0)
    window.convert.set_active(False)
    assert not window.session.convert_images
    assert "without OCR" in window.preview.details.get_label()
    assert "In “Documents”: Documents_1.png" in window.preview.details.get_label()


def test_drop_sends_and_unselects(window, cfg):
    select(window, 0, 1)
    dropped = window.session.items[1]
    window.destinations.emit("send", cfg.destinations[1], [dropped.id])
    assert dropped.state == QUEUED
    assert window.grid.selected() == [window.session.items[0]]  # still selectable, still selected
    wait_until(lambda: not window.session.busy)
    assert [p.name for p in window.session.outputs()] == ["Invoices_1.pdf"]


def test_click_with_empty_selection_does_nothing(window, cfg):
    window.destinations.emit("send", cfg.destinations[0], None)
    assert not window.session.busy


def test_send_all_then_summary(window, cfg):
    window.destinations.emit("send", window.destinations.buttons[0].dest, [i.id for i in window.session.items])
    wait_until(lambda: window.stack.get_visible_child_name() == "summary")
    assert window.summary.get_title() == "4 searchable PDFs created"
    assert "closes in 10 seconds" in window.summary.get_description()


def test_summary_counts_images_sent_without_ocr(window, cfg):
    window.convert.set_active(False)
    window.destinations.emit("send", cfg.destinations[1], [i.id for i in window.session.items])
    wait_until(lambda: window.stack.get_visible_child_name() == "summary")
    assert window.summary.get_title() == "4 files sent"
    assert sorted(p.name for p in window.session.outputs())[0] == "Invoices_1.png"


def test_other_folder(window, tmp_path):
    folder = tmp_path / "Elsewhere"
    dest = window.folder_destination(folder)
    assert (dest.name, dest.path) == ("Elsewhere", str(folder))
    window.send(window.session.items[:1], dest)
    wait_until(lambda: not window.session.busy)
    assert [p.name for p in window.session.outputs()] == ["Elsewhere_1.pdf"]


def test_other_folder_with_nothing_selected_asks_nothing(window):
    window.destinations.emit("choose", None)
    assert window.get_visible_dialog() is None


def test_delete(window):
    a = window.session.items[0]
    window._delete([a])
    assert not a.path.exists() and a not in window.session.items


def test_ocr_error_is_shown_not_raised(window, cfg):
    window.session.languages = ["klingon"]
    select(window, 0)
    window.destinations.emit("send", cfg.destinations[0], None)
    assert window.get_visible_dialog() is not None
    assert not window.session.busy


def test_drag_icon_is_small_and_cached():
    from flypage.ui.thumbnail_grid import drag_icon

    icon = drag_icon(3)
    assert (icon.get_width(), icon.get_height()) == (48, 48)
    assert drag_icon(3) is icon
    assert drag_icon(1) is not icon  # no badge for a single file


class FakeApp:
    def __init__(self, report_to_daemon):
        self.report_to_daemon = report_to_daemon
        self.sent = []

    def send_notification(self, ident, notification):
        self.sent.append(ident)


def test_report_goes_to_the_daemon_once(window, cfg, capsys, monkeypatch):
    from flypage.notifications import parse_report

    app = FakeApp(report_to_daemon=True)
    monkeypatch.setattr(window, "get_application", lambda: app)
    window.destinations.emit("send", cfg.destinations[1], [i.id for i in window.session.items])
    wait_until(lambda: window.stack.get_visible_child_name() == "summary")
    window._really_close()  # closing after the end does not report twice
    out = capsys.readouterr().out
    assert out.count("flypage-report:") == 1
    assert parse_report(out)["created"] == 4
    assert app.sent == []


def test_sort_window_notifies_itself(window, cfg, monkeypatch):
    app = FakeApp(report_to_daemon=False)
    monkeypatch.setattr(window, "get_application", lambda: app)
    window.destinations.emit("send", cfg.destinations[1], [window.session.items[0].id])
    wait_until(lambda: not window.session.busy)
    window._really_close()  # closed early: what was created is still reported
    assert app.sent == ["flypage-done"]


def test_bring_to_front_never_fails(window):
    from flypage.ui.activation import bring_to_front

    bring_to_front(window)  # on Broadway/Wayland: just present()


def test_time_left_wording():
    from flypage.ui.processing_window import time_left

    assert time_left(3) == "about 5 s left"
    assert time_left(41) == "about 45 s left"
    assert time_left(150) == "about 2 min left"
    assert time_left(2, overdue=True) == "almost done…"


def test_progress_bar_and_ring_while_running(cfg, samples):
    from conftest import SlowEngine

    session = Session(cfg, [samples.png("a.png"), samples.png("b.png")], engine=SlowEngine())
    win = ProcessingWindow(session, "Test")
    try:
        a, b = session.items
        win.send([a, b], cfg.destinations[0])
        wait_until(lambda: a.progress >= 0 or b.progress >= 0)
        assert win.update_progress()
        assert "0 / 2 pages" in win.progress.get_text() and "left" in win.progress.get_text()
    finally:
        session.cancel()
        wait_until(lambda: not session.busy)
        win.destroy()


def test_preview_says_a_digital_pdf_gets_no_ocr(cfg, samples):
    session = Session(cfg, [samples.digital_pdf()], engine=FakeEngine())
    win = ProcessingWindow(session, "Test")
    try:
        win.grid.selection.select_item(0, True)
        assert "already has text" in win.preview.details.get_label()
    finally:
        session.cancel()
        win.destroy()


def test_destination_icon_from_a_file_is_shown_on_queued_files(tmp_path):
    from flypage.ui.widgets import icon_image, set_icon

    svg = tmp_path / "books.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16"/></svg>')
    image = Gtk.Image()
    set_icon(image, str(svg))
    assert image.get_storage_type() == Gtk.ImageType.PAINTABLE  # loaded from the file, not looked up by name
    set_icon(image, "folder-documents-symbolic")
    assert image.get_icon_name() == "folder-documents-symbolic"
    set_icon(image, str(tmp_path / "gone.svg"))
    assert image.get_icon_name() == "folder-symbolic"
    assert icon_image(str(svg)).get_storage_type() == Gtk.ImageType.PAINTABLE
