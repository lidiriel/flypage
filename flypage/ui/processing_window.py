"""Processing window: sort scans into folders; dropping on a folder starts OCR (PLAN.md §6, §9)."""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from flypage import notifications, thumbnails
from flypage.config import Destination
from flypage.i18n import _, ngettext
from flypage.model import ImportedFile
from flypage.ocr import OcrError
from flypage.languages import language_name
from flypage.session import Session
from flypage.ui.activation import bring_to_front_when_shown
from flypage.ui.destination_bar import DestinationBar
from flypage.ui.thumbnail_grid import ThumbnailGrid
from flypage.ui.widgets import contract_home, screen_size, small_screen

log = logging.getLogger(__name__)

CLOSE_AFTER_S = 10  # summary shown this long before the window closes by itself
PROGRESS_TICK_MS = 500  # the estimated progress is redrawn this often while OCR runs

_css_loaded = False


def load_css() -> None:
    global _css_loaded
    if not _css_loaded:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _css_loaded = True


def time_left(seconds: float, overdue: bool = False) -> str:
    """ "about 40 s left", "about 3 min left"; "almost done…" once past the estimate."""
    if overdue and seconds < 5:
        return _("almost done…")
    if seconds < 60:
        n = max(5, int(-(-seconds // 5) * 5))  # rounded up to 5 s
        return ngettext("about {n} s left", "about {n} s left", n).format(n=n)
    n = round(seconds / 60)
    return ngettext("about {n} min left", "about {n} min left", n).format(n=n)


class PreviewPane(Gtk.Stack):
    """Large view of the selected file, with its details and future file name."""

    def __init__(self, window: ProcessingWindow) -> None:
        super().__init__(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True)
        self.window = window
        self._showing: ImportedFile | None = None

        self.add_named(
            Adw.StatusPage(icon_name="image-x-generic-symbolic", description=_("Select a file to preview it")), "none"
        )
        self.multi = Adw.StatusPage(icon_name="edit-select-all-symbolic")
        self.add_named(self.multi, "multi")

        m = 6 if small_screen() else 12
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=m, margin_bottom=m,
                      margin_start=m, margin_end=m)  # fmt: skip
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, vexpand=True, css_classes=["preview-picture"])
        double = Gtk.GestureClick()
        double.connect("pressed", lambda _g, n, _x, _y: n == 2 and window.toggle_full_preview())
        self.picture.add_controller(double)
        self.title = Gtk.Label(css_classes=["heading"], ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.details = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        box.append(self.picture)
        box.append(self.title)
        box.append(self.details)
        self.add_named(box, "file")

    def show_items(self, items: list[ImportedFile]) -> None:
        session = self.window.session
        if not items:
            self.set_visible_child_name("none")
            self._showing = None
            return
        if len(items) > 1:
            self.multi.set_title(ngettext("{n} file selected", "{n} files selected", len(items)).format(n=len(items)))
            lines = [_("Each file is sent on its own. Drag them onto a folder, or click a folder.")]
            images = sum(1 for i in items if session.passthrough(i) and not i.digital)
            if images:
                lines.append(self.window.passthrough_text(images))
            digital = sum(1 for i in items if i.digital)
            if digital:
                lines.append(self.window.digital_text(digital))
            self.multi.set_description("\n\n".join(lines))
            self.set_visible_child_name("multi")
            self._showing = None
            return

        item = items[0]
        self.set_visible_child_name("file")
        if self._showing is not item:
            self._showing = item
            self.picture.set_paintable(None)
            self.window.grid.thumbnailer.get(item.path, thumbnails.PREVIEW, lambda t, it=item: self._set(it, t))
        self.title.set_label(item.name)
        lines = [ngettext("{n} page", "{n} pages", item.pages).format(n=item.pages)]
        first = self.window.cfg.destinations[0] if self.window.cfg.destinations else None
        name = session.preview_name(item, first) if first else None
        if name:
            lines.append(_("In “{folder}”: {name}").format(folder=first.name, name=name))
        if item.digital:
            lines.append(self.window.digital_text(1))
        elif session.passthrough(item):
            lines.append(self.window.passthrough_text(1))
        if item.error:
            lines.append(_("Failed: {error}").format(error=item.error))
        self.details.set_label("\n".join(lines))

    def _set(self, item: ImportedFile, texture) -> None:
        if item is self._showing:
            self.picture.set_paintable(texture)


class ProcessingWindow(Adw.ApplicationWindow):
    def __init__(self, session: Session, source: str, **kwargs) -> None:
        # Fit the screen, leaving room for panels and the title bar (1366×768 → 1100×648).
        width, height = screen_size()
        super().__init__(
            title=_("Flypage"), default_width=min(1100, width - 60), default_height=min(780, height - 120), **kwargs
        )
        load_css()
        if small_screen():
            self.add_css_class("compact")
        self.session = session
        self.cfg = session.cfg
        self.source = source
        self._close_timer = 0
        self._close_in = 0
        self._closing = False
        self._reported = False
        self._last_folder: Gio.File | None = None
        self._tick_source = 0
        self._current = ""  # file being written, for the progress text

        # Header: icon + status message, nothing else.
        self.message = Gtk.Label(ellipsize=Pango.EllipsizeMode.END)
        title = Gtk.Box(spacing=8)
        title.append(Gtk.Image(icon_name="scanner-symbolic"))
        title.append(self.message)
        header = Adw.HeaderBar(title_widget=title)

        self.grid = ThumbnailGrid(session)
        self.grid.connect("selection-changed", self._on_selection_changed)
        self.grid.connect("item-activated", lambda *_args: self.toggle_full_preview())
        self.preview = PreviewPane(self)
        self.paned = Gtk.Paned(
            start_child=self.grid, end_child=self.preview, shrink_start_child=False, shrink_end_child=False,
            resize_start_child=True, resize_end_child=True, position=560 if small_screen() else 620, vexpand=True,
        )  # fmt: skip

        self.destinations = DestinationBar(self.cfg)
        self.destinations.connect("send", self._on_send)
        self.destinations.connect("choose", self._on_choose)

        self.main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main.append(self.paned)
        self.main.append(Gtk.Separator())
        self.main.append(self.destinations)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.main, "sort")
        self.summary = Adw.StatusPage(icon_name="object-select-symbolic")
        self.stack.add_named(self.summary, "summary")

        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.bottom_bar = self._build_toolbar()
        view.add_bottom_bar(self.bottom_bar)
        self.set_content(view)

        self._add_actions()
        session.connect("progress", self._on_progress)
        session.connect("document-done", self._on_document_done)
        session.connect("finished", self._on_finished)
        session.connect("warning", lambda _s, message: self.toast(message))
        session.store.connect("items-changed", lambda *_args: self._update_message())
        self.connect("close-request", self._on_close_request)
        bring_to_front_when_shown(self)
        self._update_message()
        self._on_selection_changed(self.grid)
        self._on_progress(session, 0, 0, "")

    def _build_toolbar(self) -> Gtk.Box:
        """One row: what the next send does (convert, move/copy, language), progress, Close."""
        self.convert = Gtk.Switch(active=self.session.convert_images, valign=Gtk.Align.CENTER)
        self.convert.connect("notify::active", self._on_convert_changed)
        convert_label = Gtk.Label(label=_("_Images to PDF"), use_underline=True, mnemonic_widget=self.convert)
        convert = Gtk.Box(spacing=6)
        convert.append(convert_label)
        convert.append(self.convert)
        convert.set_tooltip_text(
            _("On: JPG and PNG files are converted to PDF, then OCR makes them searchable.\n"
              "Off: they are sent as they are, without OCR. OCR only works on PDF.")
        )  # fmt: skip

        self.transfer = Adw.ToggleGroup()
        self.transfer.add(Adw.Toggle(name="move", label=_("Move"),
                                     tooltip=_("The originals are deleted once sent (on the scanner too)")))  # fmt: skip
        self.transfer.add(Adw.Toggle(name="copy", label=_("Copy"), tooltip=_("The originals are kept")))
        self.transfer.set_active_name("copy" if self.session.copy else "move")
        self.transfer.connect("notify::active-name", self._on_transfer_changed)

        # Configured languages in order (the first is the default), then all of them together.
        codes = list(self.cfg.ocr.languages)
        self.language_choices = [[code] for code in codes] + ([codes] if len(codes) > 1 else [])
        labels = [language_name(code) for code in codes] + ([_("All Languages")] if len(codes) > 1 else [])
        self.language = Gtk.DropDown(
            model=Gtk.StringList.new(labels), valign=Gtk.Align.CENTER, sensitive=len(labels) > 1,
            tooltip_text=_("Language of the documents, for OCR. The first one in Settings is the default."),
        )  # fmt: skip
        self.language.connect("notify::selected", self._on_language_changed)
        self._on_language_changed(self.language, None)

        self.progress = Gtk.ProgressBar(show_text=True, hexpand=True, valign=Gtk.Align.CENTER)
        close = Gtk.Button(label=_("_Close"), use_underline=True, tooltip_text=_("Close the window (running OCR stops)"))
        close.connect("clicked", lambda _b: self.close())

        margin = 3 if small_screen() else 6
        bar = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=margin, margin_bottom=margin)
        for widget in (convert, self.transfer, self.language, self.progress, close):
            bar.append(widget)
        return bar

    # --- actions ---------------------------------------------------------------

    def _add_actions(self) -> None:
        def add(name, callback, parameter=None, accels=()):
            action = Gio.SimpleAction.new(name, parameter)
            action.connect("activate", callback)
            self.add_action(action)
            if accels and self.get_application():
                self.get_application().set_accels_for_action(f"win.{name}", list(accels))
            return action

        self.delete_action = add("delete", lambda *_a: self.confirm_delete(), accels=["Delete"])

    def _on_selection_changed(self, _grid) -> None:
        selected = self.grid.selected()
        self.delete_action.set_enabled(bool(selected))
        self.preview.show_items(selected)

    def _on_convert_changed(self, switch: Gtk.Switch, _pspec) -> None:
        self.session.convert_images = switch.get_active()
        if not switch.get_active():
            self.toast(_("JPG and PNG files are now sent as they are, without OCR: OCR only works on PDF."))
        self._on_selection_changed(self.grid)

    def _on_transfer_changed(self, toggles: Adw.ToggleGroup, _pspec) -> None:
        self.session.copy = toggles.get_active_name() == "copy"
        self._on_selection_changed(self.grid)

    def _on_language_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        index = dropdown.get_selected()
        if 0 <= index < len(self.language_choices):
            self.session.languages = list(self.language_choices[index])

    def passthrough_text(self, n: int) -> str:
        """Why ``n`` images will not be OCR'd (the conversion switch is off)."""
        if self.session.copy:
            text = ngettext("Not converted: copied as it is, without OCR (OCR only works on PDF).",
                            "{n} images not converted: copied as they are, without OCR (OCR only works on PDF).", n)  # fmt: skip
        else:
            text = ngettext("Not converted: moved as it is, without OCR (OCR only works on PDF).",
                            "{n} images not converted: moved as they are, without OCR (OCR only works on PDF).", n)  # fmt: skip
        return text.format(n=n)

    @staticmethod
    def digital_text(n: int) -> str:
        """Why ``n`` PDFs will not be OCR'd: they already have text."""
        return ngettext("Digital PDF: it already has text, so it is filed as it is, without OCR.",
                        "{n} digital PDFs: they already have text, so they are filed as they are, without OCR.",
                        n).format(n=n)  # fmt: skip

    def toggle_full_preview(self) -> None:
        self.grid.set_visible(not self.grid.get_visible())

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message)))

    # --- delete ----------------------------------------------------------------

    def confirm_delete(self) -> None:
        items = self.grid.selected()
        if not items:
            return
        dialog = Adw.AlertDialog(
            heading=ngettext("Delete {n} file?", "Delete {n} files?", len(items)).format(n=len(items)),
            body=_("They are deleted from the scanner memory. This cannot be undone.")
            if self.session.originals is not None
            else _("They are deleted from the disk. This cannot be undone."),
        )
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("delete", _("_Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: response == "delete" and self._delete(items))
        dialog.present(self)

    def _delete(self, items: list[ImportedFile]) -> None:
        for error in self.session.delete(items):
            self.toast(error)

    # --- send ------------------------------------------------------------------

    def _items(self, ids: list[int] | None) -> list[ImportedFile]:
        """The dropped files, or the selection for a click."""
        return self.grid.selected() if ids is None else self.session.by_id(ids)

    def _on_send(self, _bar, dest: Destination, ids: list[int] | None) -> None:
        self.send(self._items(ids), dest)

    def send(self, items: list[ImportedFile], dest: Destination) -> None:
        items = [i for i in items if i.sortable]
        if not items:
            self.toast(_("Select files first, or drag them onto a folder."))
            return
        try:
            if self.session.send(items, dest) == 0:
                return
        except OcrError as err:
            self._alert(_("OCR cannot start"), str(err))
            return
        self.grid.unselect(items)
        self._update_message()

    def _on_choose(self, _bar, ids: list[int] | None) -> None:
        items = self._items(ids)
        if not items:
            self.toast(_("Select files first, or drag them onto a folder."))
            return
        dialog = Gtk.FileDialog(title=_("Send to Folder"), accept_label=_("_Send Here"), modal=True)
        start = self._last_folder or Gio.File.new_for_path(str(Path.home()))
        if start.query_exists(None):
            dialog.set_initial_folder(start)
        dialog.select_folder(self, None, self._on_folder_chosen, items)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, items: list[ImportedFile]) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:  # dismissed
            return
        if folder is None or folder.get_path() is None:
            return
        self._last_folder = folder
        self.send(items, self.folder_destination(Path(folder.get_path())))

    @staticmethod
    def folder_destination(path: Path) -> Destination:
        return Destination(path=str(path))  # the folder's own icon

    def _on_progress(self, _session, _done: int, _total: int, current: str) -> None:
        self._current = current
        self.update_progress()
        if self.session.busy and not self._tick_source:
            self._tick_source = GLib.timeout_add(PROGRESS_TICK_MS, self._on_progress_tick)

    def _on_progress_tick(self) -> bool:
        if self.update_progress():
            return GLib.SOURCE_CONTINUE
        self._tick_source = 0
        return GLib.SOURCE_REMOVE

    def update_progress(self) -> bool:
        """Show the estimated progress; False once nothing is queued."""
        snapshot = self.session.estimate()
        if snapshot is None:
            self.progress.set_fraction(0)
            self.progress.set_text(_("Drag files onto a folder to start"))
            return False
        self.progress.set_fraction(snapshot.fraction)
        parts = [_("{done} / {total} pages").format(done=snapshot.pages_done, total=snapshot.pages_total)]
        parts.append(time_left(snapshot.seconds_left, snapshot.overdue))
        if self._current:
            parts.append(self._current)
        self.progress.set_text(" · ".join(parts))
        return True

    def _on_document_done(self, _session, result) -> None:
        if not result.ok and not result.cancelled:
            name = result.document.sources[0].name
            self.toast(_("{name}: failed").format(name=name))
        self._update_message()
        self._on_selection_changed(self.grid)

    def _update_message(self) -> None:
        items = self.session.items
        waiting = [i for i in items if i.sortable]
        failed = [i for i in items if i.error]
        if self.session.busy and not waiting:
            text = _("Creating searchable PDFs…")
        elif failed:
            text = ngettext("{n} file failed — send it again or delete it",
                            "{n} files failed — send them again or delete them", len(failed)).format(n=len(failed))  # fmt: skip
        else:
            text = ngettext("{n} file from {source} — drag it onto a folder",
                            "{n} files from {source} — drag them onto a folder", len(waiting)).format(
                n=len(waiting), source=self.source)  # fmt: skip
        self.message.set_label(text)

    # --- end -------------------------------------------------------------------

    def _on_finished(self, _session) -> None:
        outputs = self.session.outputs()
        if not outputs:
            self.close()
            return
        folders = Counter(p.parent for p in outputs)
        title = notifications.title(len(outputs), notifications.count_pdfs(outputs))
        self.summary.set_title(title)
        self._summary_lines = "\n".join(
            ngettext("{n} in {folder}", "{n} in {folder}", count).format(n=count, folder=contract_home(str(folder)))
            for folder, count in folders.items()
        )
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        for folder in folders:
            button = Gtk.Button(label=_("Open {folder}").format(folder=folder.name), css_classes=["pill", "suggested-action"])
            button.connect("clicked", lambda _b, f=folder: self._open_folder_and_close(f))
            buttons.append(button)
        close = Gtk.Button(label=_("_Close"), use_underline=True, css_classes=["pill"])
        close.connect("clicked", lambda _b: self.close())
        buttons.append(close)
        self.summary.set_child(buttons)
        self.stack.set_visible_child_name("summary")
        self.bottom_bar.set_visible(False)
        self.message.set_label(title)
        self.report()
        self._close_in = CLOSE_AFTER_S
        self._show_countdown()
        self._close_timer = GLib.timeout_add_seconds(1, self._tick)

    def report(self) -> None:
        """Tell the user what was created, once: through the daemon if it started us, else ourselves."""
        if self._reported:
            return
        self._reported = True
        outputs = self.session.outputs()
        app = self.get_application()
        if getattr(app, "report_to_daemon", False):
            print(notifications.report_line(outputs), flush=True)
        elif app is not None and outputs:
            folders = list(dict.fromkeys(str(p.parent) for p in outputs))
            summary = notifications.summary(len(outputs), folders, pdfs=notifications.count_pdfs(outputs))
            app.send_notification("flypage-done", summary)

    def _open_folder_and_close(self, folder: Path) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)
        except GLib.Error as err:
            log.warning("cannot open %s: %s", folder, err.message)
        self.close()

    def _show_countdown(self) -> None:
        countdown = ngettext("This window closes in {s} second.", "This window closes in {s} seconds.", self._close_in)
        self.summary.set_description(self._summary_lines + "\n\n" + countdown.format(s=self._close_in))

    def _tick(self) -> bool:
        self._close_in -= 1
        if self._close_in > 0:
            self._show_countdown()
            return GLib.SOURCE_CONTINUE
        self._close_timer = 0
        self.close()
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _window) -> bool:
        if self._closing:
            return False
        if self.session.busy:
            dialog = Adw.AlertDialog(
                heading=_("Stop OCR?"),
                body=_("The files being processed stay where they are. PDFs already created are kept."),
            )
            dialog.add_response("continue", _("_Continue"))
            dialog.add_response("stop", _("_Stop and Close"))
            dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("continue")
            dialog.set_close_response("continue")
            dialog.connect("response", lambda _d, response: response == "stop" and self._really_close())
            dialog.present(self)
            return True
        self._really_close()
        return True

    def _really_close(self) -> None:
        if self._tick_source:
            GLib.source_remove(self._tick_source)
            self._tick_source = 0
        if self._close_timer:
            GLib.source_remove(self._close_timer)
            self._close_timer = 0
        self.session.cancel()
        self.report()  # closed before the end: still report the PDFs already created
        self._closing = True
        self.close()

    def _alert(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", _("_OK"))
        dialog.present(self)
