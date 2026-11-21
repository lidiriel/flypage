"""Processing window behaviour. Widgets are built but not shown; needs a display."""

import pytest

import flypage.ui  # noqa: F401
from gi.repository import Adw, GLib, Gtk

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from conftest import FakeEngine  # noqa: E402
from test_session import wait_until  # noqa: E402

from flypage import config  # noqa: E402
from flypage.config import Config, Destination  # noqa: E402
from flypage.model import QUEUED  # noqa: E402
from flypage.session import ALL_SAME, PER_FILE, Session  # noqa: E402
from flypage.ui.thumbnail_grid import drag_payload, parse_payload  # noqa: E402
from flypage.ui.processing_window import ProcessingWindow  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    c = Config(
        destinations=[
            Destination(name="Documents", path=str(tmp_path / "Documents"), default=True),
            Destination(name="Invoices", path=str(tmp_path / "Invoices")),
        ]
    )
    c.naming.template = "%{dest}_%n"
    config.save(c)
    return c


@pytest.fixture
def window(cfg, samples):
    files = [samples.png(f"s{i}.png", f"Page {i}") for i in range(4)]
    session = Session(cfg, files, engine=FakeEngine(), sort_mode=PER_FILE)
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


def test_message_and_buttons_follow_the_selection(window):
    assert "4 files from Test" in window.message.get_label()
    assert not window.chain_action.get_enabled() and not window.delete_action.get_enabled()
    select(window, 0)
    assert window.delete_action.get_enabled() and not window.chain_action.get_enabled()
    select(window, 0, 2)
    assert window.chain_action.get_enabled()


def test_chain_keeps_the_chain_selected(window):
    select(window, 0, 2)
    window.activate_action("win.chain")
    a, c = window.session.items[0], window.session.items[1]
    assert a.chain and a.chain == c.chain
    assert window.grid.selected() == [a, c]
    assert window.preview.title.get_label() == "Chain 1"


def test_unchain_from_menu_action(window):
    select(window, 0, 1)
    window.activate_action("win.chain")
    item = window.session.items[0]
    window.activate_action("win.unchain", GLib.Variant.new_int32(item.id))
    assert item.chain == 0


def test_drop_sends_and_unselects(window, cfg):
    select(window, 0, 1)
    dropped = window.session.items[1]
    window.destinations.emit("send", cfg.destinations[1], [dropped.id])
    assert dropped.state == QUEUED
    assert window.grid.selected() == [window.session.items[0]]  # still selectable, still selected
    wait_until(lambda: not window.session.busy)
    assert [p.name for p in window.session.outputs()] == ["Invoices_1.pdf"]


def test_click_with_empty_selection_in_per_file_mode_does_nothing(window, cfg):
    window.destinations.emit("send", cfg.destinations[0], None)
    assert not window.session.busy


def test_mode_toggle_is_saved(window):
    window.mode.set_active_name(ALL_SAME)
    assert window.session.sort_mode == ALL_SAME
    assert config.load().processing.sort_mode == ALL_SAME


def test_all_same_then_summary(window, cfg):
    window.mode.set_active_name(ALL_SAME)
    window.destinations.emit("send", window.destinations.buttons[0].dest, [window.session.items[0].id])
    wait_until(lambda: window.stack.get_visible_child_name() == "summary")
    assert window.summary.get_title() == "4 searchable PDFs created"
    assert "closes in 10 seconds" in window.summary.get_description()


def test_delete(window):
    a = window.session.items[0]
    window._delete([a])
    assert not a.path.exists() and a not in window.session.items


def test_ocr_error_is_shown_not_raised(window, cfg):
    window.session.cfg.ocr.languages = ["klingon"]
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
    window.mode.set_active_name(ALL_SAME)
    window.destinations.emit("send", cfg.destinations[1], [window.session.items[0].id])
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
