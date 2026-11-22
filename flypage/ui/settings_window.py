"""Settings window (PLAN.md §5). Every valid change is saved right away."""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from flypage import config
from flypage.config import Destination, Scanner
from flypage.i18n import SUPPORTED_UI_LANGUAGES, _
from flypage.languages import language_name
from flypage.ocr.tesseract import TesseractEngine
from flypage.ui.dialogs import DestinationDialog, ScannerDialog
from flypage.ui.template_editor import TemplateEditor
from flypage.ui.widgets import icon_image

log = logging.getLogger(__name__)

SAVE_DELAY_MS = 300  # typing in a field saves once, shortly after the last key


class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(title=_("Flypage Settings"), default_width=800, default_height=560, **kwargs)
        self.cfg = config.load()
        self._save_source = 0

        stack = Adw.ViewStack()
        pages = (DestinationsPage(self), NamingPage(self), OcrPage(self), ScannersPage(self), InterfacePage(self))
        self.pages = {page.name: page for page in pages}
        for page in pages:
            stack.add_titled_with_icon(page, page.name, page.get_title(), page.get_icon_name())

        self.toasts = Adw.ToastOverlay(child=stack)
        header = Adw.HeaderBar(title_widget=Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        switcher_bar = Adw.ViewSwitcherBar(stack=stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_bottom_bar(switcher_bar)
        self.set_content(view)

        # Narrow window: page switcher moves to the bottom.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        narrow.add_setter(header, "title-widget", Adw.WindowTitle(title=self.get_title()))
        narrow.add_setter(switcher_bar, "reveal", True)
        self.add_breakpoint(narrow)

        self.connect("close-request", self._on_close)

    def changed(self) -> None:
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(SAVE_DELAY_MS, self._save_now)

    def _save_now(self) -> bool:
        self._save_source = 0
        try:
            config.save(self.cfg)
        except OSError as err:
            log.warning("cannot save settings: %s", err)
            self.toast(_("Cannot save the settings: {error}").format(error=err.strerror or err))
        return GLib.SOURCE_REMOVE

    def flush(self) -> None:
        """Write a pending change now."""
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save_now()

    def _on_close(self, _window) -> bool:
        self.flush()
        return False

    def toast(self, message: str, **kwargs) -> Adw.Toast:
        toast = Adw.Toast(title=GLib.markup_escape_text(message), **kwargs)
        self.toasts.add_toast(toast)
        return toast


class _Page(Adw.PreferencesPage):
    name = ""

    def __init__(self, window: SettingsWindow, title: str, icon: str) -> None:
        super().__init__(title=title, icon_name=icon)
        self.window = window

    @property
    def cfg(self) -> config.Config:
        return self.window.cfg


class _ListGroup(Adw.PreferencesGroup):
    """A group whose rows are rebuilt from the configuration on each change."""

    def __init__(self, title: str, description: str, add_tooltip: str, on_add) -> None:
        super().__init__(title=title, description=description)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=add_tooltip, css_classes=["flat"])
        add.connect("clicked", lambda _button: on_add())
        self.set_header_suffix(add)
        self._rows: list[Gtk.Widget] = []

    def set_rows(self, rows: list[Gtk.Widget]) -> None:
        for row in self._rows:
            self.remove(row)
        self._rows = rows
        for row in rows:
            self.add(row)


def _icon_button(icon: str, tooltip: str, callback, *args, sensitive: bool = True) -> Gtk.Button:
    button = Gtk.Button(
        icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER, css_classes=["flat"], sensitive=sensitive
    )
    button.connect("clicked", lambda _button: callback(*args))
    return button


# --- Destinations ---------------------------------------------------------------


class DestinationsPage(_Page):
    name = "destinations"

    def __init__(self, window: SettingsWindow) -> None:
        super().__init__(window, _("Destinations"), "folder-symbolic")
        self.group = _ListGroup(
            _("Destination Folders"),
            _(
                "Shown as icons in the processing window, in this order. Each one is named after its "
                "folder; “Other Folder…” in the window sends anywhere else."
            ),
            _("Add a destination"),
            self._add,
        )
        self.add(self.group)
        self.refresh()

    def refresh(self) -> None:
        dests = self.cfg.destinations
        rows = []
        for i, dest in enumerate(dests):
            subtitle = dest.path + (f"  ·  {dest.template}" if dest.template else "")
            if dest.table_layout:
                subtitle += "  ·  " + _("tables")
            row = Adw.ActionRow(title=dest.name, subtitle=subtitle, use_markup=False, activatable=True)
            row.add_prefix(icon_image(dest.icon))
            row.add_suffix(_icon_button("go-up-symbolic", _("Move up"), self._move, i, -1, sensitive=i > 0))
            row.add_suffix(_icon_button("go-down-symbolic", _("Move down"), self._move, i, 1, sensitive=i < len(dests) - 1))
            row.add_suffix(
                _icon_button("user-trash-symbolic", _("Remove"), self._remove, i, sensitive=len(dests) > 1)
            )
            row.connect("activated", lambda _row, d=dest: self._edit(d))
            rows.append(row)
        self.group.set_rows(rows)

    def _taken(self, exclude: Destination | None = None) -> list[Destination]:
        """The other destinations: a folder can be listed once only."""
        return [d for d in self.cfg.destinations if d is not exclude]

    def _add(self) -> None:
        def save(dest: Destination) -> None:
            self.cfg.destinations.append(dest)
            self._commit()

        DestinationDialog(None, self._taken(), self.cfg.naming.template, save).present(self.window)

    def _edit(self, dest: Destination) -> None:
        def save(edited: Destination) -> None:
            index = self.cfg.destinations.index(dest)
            self.cfg.destinations[index] = edited
            self._commit()

        DestinationDialog(dest, self._taken(dest), self.cfg.naming.template, save).present(self.window)

    def _move(self, index: int, step: int) -> None:
        dests = self.cfg.destinations
        dests[index], dests[index + step] = dests[index + step], dests[index]
        self._commit()

    def _remove(self, index: int) -> None:
        dests = self.cfg.destinations
        removed = dests.pop(index)
        self._commit()

        def undo(_toast) -> None:
            dests.insert(min(index, len(dests)), removed)
            self._commit()

        toast = self.window.toast(_("“{name}” removed").format(name=removed.name), button_label=_("_Undo"))
        toast.connect("button-clicked", undo)

    def _commit(self) -> None:
        self.refresh()
        self.window.changed()


# --- Naming ---------------------------------------------------------------------


class NamingPage(_Page):
    name = "naming"

    def __init__(self, window: SettingsWindow) -> None:
        super().__init__(window, _("Naming"), "document-edit-symbolic")
        editor = TemplateEditor(
            self.cfg.naming.template,
            self.cfg.destinations[0].name,
            title=_("Output File Name"),
            description=_(
                "Used for every destination without a template of its own. Click a code to insert it. "
                "Other date codes work too, e.g. %b (month name), %a (weekday), %y (2-digit year), "
                "%-d (day without leading zero); write %% for a percent sign. "
                "“.pdf” is added, and “-1”, “-2”… if the name is already taken."
            ),
        )
        editor.connect("template-changed", self._on_template_changed)
        self.add(editor)
        self.editor = editor

    def _on_template_changed(self, _editor, template: str) -> None:
        if template != self.cfg.naming.template:
            self.cfg.naming.template = template
            self.window.changed()


# --- OCR ------------------------------------------------------------------------


class OcrPage(_Page):
    name = "ocr"

    def __init__(self, window: SettingsWindow) -> None:
        super().__init__(window, _("OCR"), "accessories-text-editor-symbolic")
        self._reverting = False
        self.checks: dict[str, Gtk.CheckButton] = {}
        tesseract = TesseractEngine()
        installed = tesseract.languages() if tesseract.available() else []

        engines = Adw.PreferencesGroup(title=_("Engine"))
        engines.add(
            Adw.ActionRow(
                title="Tesseract",
                subtitle=(
                    _("Fast, runs on any computer")
                    if tesseract.available()
                    else _("Not installed: sudo apt install ocrmypdf")
                ),
            )
        )
        self.add(engines)

        self.languages = Adw.PreferencesGroup(
            title=_("Languages"),
            description=_(
                "Languages present in your documents, in order: the first one is the default in the "
                "processing window. Each extra language used together slows OCR down. To add one, "
                "install its Tesseract package, e.g. “sudo apt install tesseract-ocr-deu” for German."
            ),
        )
        self.installed = installed
        self._rows: list[Gtk.Widget] = []
        self.add(self.languages)
        self.refresh()

        advanced = Adw.PreferencesGroup(
            title=_("Advanced"),
            description=_("Off by default: they make OCR slower. Tables are set per destination."),
        )
        self.rotate = Adw.SwitchRow(
            title=_("Straighten Rotated Pages"),
            subtitle=_("For pages scanned upside down or sideways (about 15 % more time)")
            if tesseract.can_rotate()
            else _("Needs the Tesseract model “osd”: sudo apt install tesseract-ocr-osd"),
            active=self.cfg.ocr.rotate_pages,
            sensitive=tesseract.can_rotate() or self.cfg.ocr.rotate_pages,
        )
        self.rotate.connect("notify::active", self._on_option, "rotate_pages")
        self.clean = Adw.SwitchRow(
            title=_("Clean Pages Before OCR"),
            subtitle=_("Helps with dirty or shadowed scans; the saved image is unchanged (about 50 % more time)")
            if tesseract.can_clean()
            else _("Needs unpaper: sudo apt install unpaper"),
            active=self.cfg.ocr.clean,
            sensitive=tesseract.can_clean() or self.cfg.ocr.clean,
        )
        self.clean.connect("notify::active", self._on_option, "clean")
        advanced.add(self.rotate)
        advanced.add(self.clean)
        self.add(advanced)

    def _on_option(self, row: Adw.SwitchRow, _pspec, name: str) -> None:
        setattr(self.cfg.ocr, name, row.get_active())
        self.window.changed()

    def refresh(self) -> None:
        """Checked languages first, in their order (with up/down buttons), then the others by name."""
        for row in self._rows:
            self.languages.remove(row)
        self._rows = []
        self.checks = {}
        chosen = self.cfg.ocr.languages
        others = sorted(set(self.installed) - set(chosen), key=lambda c: language_name(c).casefold())
        for code in chosen + others:
            parts = [code if code in self.installed else _("{code} — not installed").format(code=code)]
            if chosen and code == chosen[0]:
                parts.append(_("default"))
            row = Adw.ActionRow(title=language_name(code), subtitle="  ·  ".join(parts), use_markup=False)
            check = Gtk.CheckButton(active=code in chosen, valign=Gtk.Align.CENTER)
            check.connect("toggled", self._on_language_toggled, code)
            self.checks[code] = check
            row.add_prefix(check)
            row.set_activatable_widget(check)
            if code in chosen:
                i = chosen.index(code)
                row.add_suffix(_icon_button("go-up-symbolic", _("Move up"), self._move_language, i, -1, sensitive=i > 0))
                row.add_suffix(
                    _icon_button("go-down-symbolic", _("Move down"), self._move_language, i, 1,
                                 sensitive=i < len(chosen) - 1)
                )  # fmt: skip
            self.languages.add(row)
            self._rows.append(row)

    def _move_language(self, index: int, step: int) -> None:
        languages = self.cfg.ocr.languages
        other = index + step
        if 0 <= other < len(languages):
            languages[index], languages[other] = languages[other], languages[index]
            self.refresh()
            self.window.changed()

    def _on_language_toggled(self, check: Gtk.CheckButton, code: str) -> None:
        if self._reverting:
            return
        languages = self.cfg.ocr.languages
        if check.get_active():
            if code not in languages:
                languages.append(code)  # appended: the first ones keep priority
        elif languages == [code]:
            self._reverting = True
            check.set_active(True)
            self._reverting = False
            self.window.toast(_("At least one language is needed."))
            return
        else:
            languages.remove(code)
        # Rebuilt once this signal is over: the row being clicked is replaced.
        GLib.idle_add(lambda: self.refresh() and False)
        self.window.changed()


# --- Scanners -------------------------------------------------------------------


class ScannersPage(_Page):
    name = "scanners"

    def __init__(self, window: SettingsWindow) -> None:
        super().__init__(window, _("Scanners"), "scanner-symbolic")
        self.group = _ListGroup(
            _("Scanners with Memory"),
            _("Flypage imports the files of these scanners when they are plugged in."),
            _("Add a scanner"),
            self._add,
        )
        self.add(self.group)
        self.refresh()

    def refresh(self) -> None:
        rows = []
        for i, scanner in enumerate(self.cfg.scanners):
            ids = f"{scanner.vendor_id}:{scanner.model_id or '*'}"
            details = [ids, scanner.source_dir or _("whole memory")]
            if scanner.delete_after_import:
                details.append(_("deleted after import"))
            row = Adw.ActionRow(title=scanner.name, subtitle="  ·  ".join(details), use_markup=False, activatable=True)
            row.add_prefix(Gtk.Image(icon_name="scanner-symbolic", pixel_size=24))
            row.add_suffix(_icon_button("user-trash-symbolic", _("Remove"), self._remove, i))
            row.connect("activated", lambda _row, s=scanner: self._edit(s))
            rows.append(row)
        if not rows:
            rows.append(Adw.ActionRow(title=_("No scanner configured"), sensitive=False))
        self.group.set_rows(rows)

    def _add(self) -> None:
        def save(scanner: Scanner) -> None:
            self.cfg.scanners.append(scanner)
            self._commit()

        ScannerDialog(None, save).present(self.window)

    def _edit(self, scanner: Scanner) -> None:
        def save(edited: Scanner) -> None:
            self.cfg.scanners[self.cfg.scanners.index(scanner)] = edited
            self._commit()

        ScannerDialog(scanner, save).present(self.window)

    def _remove(self, index: int) -> None:
        removed = self.cfg.scanners.pop(index)
        self._commit()

        def undo(_toast) -> None:
            self.cfg.scanners.insert(min(index, len(self.cfg.scanners)), removed)
            self._commit()

        toast = self.window.toast(_("“{name}” removed").format(name=removed.name), button_label=_("_Undo"))
        toast.connect("button-clicked", undo)

    def _commit(self) -> None:
        self.refresh()
        self.window.changed()


# --- Interface ------------------------------------------------------------------


class InterfacePage(_Page):
    name = "interface"

    # Language names are written in their own language, so they are not translated.
    LABELS = {"en": "English", "fr": "Français"}

    def __init__(self, window: SettingsWindow) -> None:
        super().__init__(window, _("Interface"), "preferences-desktop-locale-symbolic")
        group = Adw.PreferencesGroup(title=_("Language"), description=_("Applies the next time Flypage starts."))
        labels = [_("System default") if code == "system" else self.LABELS[code] for code in SUPPORTED_UI_LANGUAGES]
        self.combo = Adw.ComboRow(title=_("Interface language"), model=Gtk.StringList.new(labels))
        self.combo.set_selected(SUPPORTED_UI_LANGUAGES.index(self.cfg.ui_language))
        self.combo.connect("notify::selected", self._on_selected)
        group.add(self.combo)
        self.add(group)

    def _on_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        self.cfg.ui_language = SUPPORTED_UI_LANGUAGES[combo.get_selected()]
        self.window.changed()
