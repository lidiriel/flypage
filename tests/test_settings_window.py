"""Settings window logic. Widgets are built but never shown; needs a display (X11/Wayland/Broadway)."""

import pytest

import flypage.ui  # noqa: F401
from gi.repository import Adw, Gdk, Gtk

# init_check() can return True with no display at all: also check there is one.
if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from flypage import config  # noqa: E402
from flypage.config import Destination, Scanner  # noqa: E402
from flypage.languages import language_name  # noqa: E402
from flypage.ui.dialogs import DestinationDialog, ScannerDialog  # noqa: E402
from flypage.ui.settings_window import SettingsWindow  # noqa: E402


@pytest.fixture
def window():
    config.save(
        config.Config(
            destinations=[
                Destination(path="~/Documents"),
                Destination(path="~/Invoices"),
                Destination(path="~/Medical"),
            ]
        )
    )
    win = SettingsWindow()
    yield win
    win.destroy()


def saved(win) -> config.Config:
    win.flush()
    return config.load()


def test_template_is_saved_only_when_valid(window):
    editor = window.pages["naming"].editor
    editor.entry.set_text("%Y-ocr-%n")
    assert saved(window).naming.template == "%Y-ocr-%n"

    editor.entry.set_text("%Y-%q")
    assert not editor.valid
    assert "error" in editor.entry.get_css_classes()
    assert saved(window).naming.template == "%Y-ocr-%n"


def test_preview_uses_the_template(window):
    editor = window.pages["naming"].editor
    editor.entry.set_text("scan-%{dest}-%3n")
    assert editor.preview.get_subtitle() == "scan-Documents-001.pdf"


def test_code_buttons_insert_at_the_end_before_any_click(window):
    editor = window.pages["naming"].editor
    editor.entry.set_text("scan-")
    editor._insert("%3n")
    assert editor.entry.get_text() == "scan-%3n"


def test_move_and_remove(window):
    page = window.pages["destinations"]
    page._move(0, 1)
    assert [d.name for d in saved(window).destinations] == ["Invoices", "Documents", "Medical"]
    page._remove(0)
    assert [d.name for d in saved(window).destinations] == ["Documents", "Medical"]


def test_last_language_cannot_be_unchecked(window):
    page = window.pages["ocr"]
    window.cfg.ocr.languages = ["eng"]
    check = page.checks["eng"]
    check.set_active(True)
    check.set_active(False)
    assert check.get_active()
    assert window.cfg.ocr.languages == ["eng"]


def test_languages_are_ordered_and_the_first_is_the_default(window):
    page = window.pages["ocr"]
    window.cfg.ocr.languages = ["eng", "fra"]
    page.refresh()
    page._move_language(1, -1)
    assert saved(window).ocr.languages == ["fra", "eng"]
    rows = page._rows
    assert rows[0].get_title() == language_name("fra") and "default" in rows[0].get_subtitle()
    page._move_language(0, -1)  # already first: nothing happens
    assert window.cfg.ocr.languages == ["fra", "eng"]


def test_interface_language(window):
    window.pages["interface"].combo.set_selected(2)
    assert saved(window).ui_language == "fr"


def test_destination_dialog_validation():
    results = []
    dialog = DestinationDialog(None, [Destination(path="~/Invoices")], "%Y_%{dest}", results.append)
    assert not dialog.save_button.get_sensitive()  # no folder
    assert not hasattr(dialog, "name_row")  # the name is the folder's

    dialog.dest.path = "~/Invoices/"
    dialog._show_folder()
    dialog.check()
    assert not dialog.save_button.get_sensitive()  # already a destination
    assert "error" in dialog.folder_row.get_css_classes()

    dialog.dest.path = "~/Taxes"
    dialog._show_folder()
    dialog.check()
    assert dialog.save_button.get_sensitive()
    assert dialog.folder_row.get_title() == "Taxes"
    assert "Taxes" in dialog.template_editor.preview.get_subtitle()  # example uses the folder's name

    dialog.own_template.set_active(True)
    dialog.template_editor.entry.set_text("%q")
    assert not dialog.save_button.get_sensitive()
    dialog.own_template.set_active(False)  # an invalid template that is not used is fine
    assert dialog.save_button.get_sensitive()

    dialog._save(None)
    assert results == [Destination(path="~/Taxes")]


def test_destination_dialog_keeps_own_template():
    results = []
    dest = Destination(path="/a", template="%Y")
    dialog = DestinationDialog(dest, [], "%n", results.append)
    assert dialog.own_template.get_active()
    dialog.template_editor.entry.set_text("%Y-%m")
    dialog._save(None)
    assert results[0].template == "%Y-%m"
    assert dest.template == "%Y"  # the original is untouched until saved by the page


def test_scanner_dialog():
    results = []
    dialog = ScannerDialog(None, results.append)
    dialog.name_row.set_text("Book")
    dialog.vendor_row.set_text("07B")
    assert not dialog.save_button.get_sensitive()
    dialog.vendor_row.set_text("07B4")
    dialog.model_row.set_text("zz")
    assert not dialog.save_button.get_sensitive()
    dialog.model_row.set_text("")
    dialog.source_row.set_text("/DCIM/")
    dialog._save(None)
    assert results == [Scanner(name="Book", vendor_id="07b4", source_dir="DCIM")]


def test_scanner_dialog_uses_a_detected_disk(tmp_path):
    from flypage.device import UsbDisk

    (tmp_path / "DCIM").mkdir()
    results = []
    dialog = ScannerDialog(None, results.append)
    dialog.use_disk(
        UsbDisk(object_path="/x", device="/dev/sdb1", vendor_id="07b4", model_id="0109", serial="9645",
                vendor="OLYMPUS", model="FE210/X775", mount_points=[str(tmp_path)])  # fmt: skip
    )
    assert dialog.save_button.get_sensitive()
    dialog._save(None)
    assert results == [Scanner(name="OLYMPUS FE210/X775", vendor_id="07b4", model_id="0109", source_dir="DCIM")]


def test_advanced_ocr_options_are_saved(window):
    page = window.pages["ocr"]
    if page.rotate.get_sensitive():
        page.rotate.set_active(True)
        assert saved(window).ocr.rotate_pages
    if page.clean.get_sensitive():
        page.clean.set_active(True)
        assert saved(window).ocr.clean


def test_destination_dialog_table_layout():
    got = []
    dialog = DestinationDialog(Destination(path="~/I"), [], "%n", got.append)
    dialog.table_layout.set_active(True)
    dialog._save(None)
    assert got[0].table_layout
