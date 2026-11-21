"""Processing window: sort scans into folders; dropping on a folder starts OCR (PLAN.md §6)."""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from flypage import config, notifications, thumbnails
from flypage.config import Destination
from flypage.i18n import _, ngettext
from flypage.model import ImportedFile
from flypage.ocr import OcrError
from flypage.session import ALL_SAME, PER_FILE, Session
from flypage.ui.activation import bring_to_front_when_shown
from flypage.ui.destination_bar import DestinationBar
from flypage.ui.thumbnail_grid import ThumbnailGrid
from flypage.ui.widgets import contract_home

log = logging.getLogger(__name__)

CLOSE_AFTER_S = 10  # summary shown this long before the window closes by itself

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

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)  # fmt: skip
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
        group = session.expand(items)
        first = items[0]
        if len(items) > 1 and not (first.chain and group == session.chain_members(first.chain)):
            self.multi.set_title(ngettext("{n} file selected", "{n} files selected", len(items)).format(n=len(items)))
            self.multi.set_description(_("Drag them onto a folder, or click a folder, to send them."))
            self.set_visible_child_name("multi")
            self._showing = None
            return

        self.set_visible_child_name("file")
        if self._showing is not first:
            self._showing = first
            self.picture.set_paintable(None)
            self.window.grid.thumbnailer.get(first.path, thumbnails.PREVIEW, lambda t, it=first: self._set(it, t))
        pages = sum(i.pages for i in group)
        if first.chain:
            self.title.set_label(_("Chain {number}").format(number=first.chain))
            lines = [ngettext("{n} file", "{n} files", len(group)).format(n=len(group)) + " · "
                     + ngettext("{n} page", "{n} pages", pages).format(n=pages)]  # fmt: skip
        else:
            self.title.set_label(first.name)
            lines = [ngettext("{n} page", "{n} pages", pages).format(n=pages)]
        name = session.preview_name(items, self.window.cfg.default_destination())
        if name:
            lines.append(_("In “Default”: {name}").format(name=name))
        if first.error:
            lines.append(_("Failed: {error}").format(error=first.error))
        self.details.set_label("\n".join(lines))

    def _set(self, item: ImportedFile, texture) -> None:
        if item is self._showing:
            self.picture.set_paintable(texture)


class ProcessingWindow(Adw.ApplicationWindow):
    def __init__(self, session: Session, source: str, **kwargs) -> None:
        super().__init__(title=_("Flypage"), default_width=1100, default_height=780, **kwargs)
        load_css()
        self.session = session
        self.cfg = session.cfg
        self.source = source
        self._close_timer = 0
        self._close_in = 0
        self._closing = False
        self._reported = False

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
            resize_start_child=True, resize_end_child=True, position=620, vexpand=True,
        )  # fmt: skip

        # Action row: Chain + Delete, and the mode toggle.
        self.chain_button = Gtk.Button(
            child=Adw.ButtonContent(icon_name="insert-link-symbolic", label=_("_Chain"), use_underline=True),
            action_name="win.chain",
            tooltip_text=_("Merge the selected files into one PDF (or unchain them)"),
        )
        self.delete_button = Gtk.Button(
            child=Adw.ButtonContent(icon_name="user-trash-symbolic", label=_("_Delete"), use_underline=True),
            action_name="win.delete",
            tooltip_text=_("Delete the selected files"),
        )
        self.mode = Adw.ToggleGroup()
        self.mode.add(Adw.Toggle(name=ALL_SAME, label=_("Same for all"),
                                 tooltip=_("A drop sends every file to that folder")))  # fmt: skip
        self.mode.add(Adw.Toggle(name=PER_FILE, label=_("Per file"), tooltip=_("A drop sends only the dragged files")))
        self.mode.set_active_name(session.sort_mode)
        self.mode.connect("notify::active-name", self._on_mode_changed)
        actions = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6)
        actions.append(self.chain_button)
        actions.append(self.delete_button)
        actions.append(Gtk.Box(hexpand=True))
        actions.append(Gtk.Label(label=_("Mode"), css_classes=["dim-label"]))
        actions.append(self.mode)

        self.destinations = DestinationBar(self.cfg)
        self.destinations.connect("send", self._on_send)

        # Bottom row: Cancel + progress.
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True, tooltip_text=_("Stop everything and close"))
        cancel.connect("clicked", lambda _b: self.close())
        self.progress = Gtk.ProgressBar(show_text=True, hexpand=True, valign=Gtk.Align.CENTER)
        bottom = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        bottom.append(cancel)
        bottom.append(self.progress)

        self.main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main.append(self.paned)
        self.main.append(Gtk.Separator())
        self.main.append(actions)
        self.main.append(self.destinations)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.main, "sort")
        self.summary = Adw.StatusPage(icon_name="object-select-symbolic")
        self.stack.add_named(self.summary, "summary")

        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_bottom_bar(bottom)
        self.bottom_bar = bottom
        self.set_content(view)

        self._add_actions()
        session.connect("progress", self._on_progress)
        session.connect("document-done", self._on_document_done)
        session.connect("finished", self._on_finished)
        session.connect("warning", lambda _s, message: self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(message))))
        session.store.connect("items-changed", lambda *_args: self._update_message())
        self.connect("close-request", self._on_close_request)
        bring_to_front_when_shown(self)
        self._update_message()
        self._on_selection_changed(self.grid)
        self._on_progress(session, 0, 0, "")

    # --- actions ---------------------------------------------------------------

    def _add_actions(self) -> None:
        def add(name, callback, parameter=None, accels=()):
            action = Gio.SimpleAction.new(name, parameter)
            action.connect("activate", callback)
            self.add_action(action)
            if accels and self.get_application():
                self.get_application().set_accels_for_action(f"win.{name}", list(accels))
            return action

        self.chain_action = add("chain", lambda *_a: self._chain(), accels=["<Ctrl>l"])
        self.delete_action = add("delete", lambda *_a: self.confirm_delete(), accels=["Delete"])
        add("unchain", self._on_unchain, GLib.VariantType.new("i"))

    def _chain(self) -> None:
        items = self.grid.selected()
        self.session.toggle_chain(items)
        # Chaining moves files next to each other, which loses the selection.
        self.grid.select(self.session.expand(items))
        self._on_selection_changed(self.grid)

    def _on_unchain(self, _action, value: GLib.Variant) -> None:
        for item in self.session.by_id([value.get_int32()]):
            self.session.remove_from_chain(item)

    def _on_selection_changed(self, _grid) -> None:
        selected = self.grid.selected()
        self.chain_action.set_enabled(len(selected) >= 2 or any(i.chain for i in selected))
        self.delete_action.set_enabled(bool(selected))
        self.preview.show_items(selected)

    def _on_mode_changed(self, toggles: Adw.ToggleGroup, _pspec) -> None:
        mode = toggles.get_active_name()
        self.session.sort_mode = mode
        try:
            config.update(lambda cfg: setattr(cfg.processing, "sort_mode", mode))
        except OSError as err:
            log.warning("cannot save the sort mode: %s", err)

    def toggle_full_preview(self) -> None:
        self.grid.set_visible(not self.grid.get_visible())

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
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(error)))

    # --- send ------------------------------------------------------------------

    def _on_send(self, _bar, dest: Destination, ids: list[int] | None) -> None:
        items = self.grid.selected() if ids is None else self.session.by_id(ids)
        if not items and self.session.sort_mode == PER_FILE:
            self.toasts.add_toast(Adw.Toast(title=_("Select files first, or drag them onto a folder.")))
            return
        try:
            sent = self.session.items_to_send(items)
            if self.session.send(items, dest) == 0:
                return
        except OcrError as err:
            self._alert(_("OCR cannot start"), str(err))
            return
        self.grid.unselect(sent)
        self._update_message()

    def _on_progress(self, _session, done: int, total: int, current: str) -> None:
        if total == 0:
            self.progress.set_fraction(0)
            self.progress.set_text(_("Drag files onto a folder to start"))
            return
        self.progress.set_fraction(done / total)
        text = _("OCR {done} / {total} pages").format(done=done, total=total)
        if current and done < total:
            text += f" — {current}"
        self.progress.set_text(text)

    def _on_document_done(self, _session, result) -> None:
        if not result.ok and not result.cancelled:
            name = result.document.sources[0].name
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(_("{name}: OCR failed").format(name=name))))
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
        title = ngettext("{n} searchable PDF created", "{n} searchable PDFs created", len(outputs)).format(n=len(outputs))
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
            app.send_notification("flypage-done", notifications.summary(len(outputs), folders))

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
