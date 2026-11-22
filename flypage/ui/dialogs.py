"""Add/edit dialogs for destinations and scanner profiles."""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Callable

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from flypage import config
from flypage.config import Destination, Scanner
from flypage.device import UDisksWatcher, UsbDisk
from flypage.i18n import _
from flypage.ui.template_editor import TemplateEditor
from flypage.ui.widgets import available_icons, contract_home, icon_image, mark_error

log = logging.getLogger(__name__)


class FormDialog(Adw.Dialog):
    """Dialog with Cancel / Save in the header; Save is enabled only while ``validate()`` is true."""

    def __init__(self, title: str, on_save: Callable, **kwargs) -> None:
        super().__init__(title=title, content_width=560, **kwargs)
        self._on_save = on_save

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        cancel.connect("clicked", lambda _button: self.close())
        self.save_button = Gtk.Button(label=_("_Save"), use_underline=True, css_classes=["suggested-action"])
        self.save_button.connect("clicked", self._save)
        header.pack_start(cancel)
        header.pack_end(self.save_button)
        self.set_default_widget(self.save_button)

        self.page = Adw.PreferencesPage()
        view = Adw.ToolbarView(content=self.page)
        view.add_top_bar(header)
        self.set_child(view)

    def check(self, *_args) -> None:
        self.save_button.set_sensitive(self.validate())

    def validate(self) -> bool:
        raise NotImplementedError

    def result(self):
        raise NotImplementedError

    def _save(self, _button) -> None:
        if self.validate():
            self._on_save(self.result())
            self.close()


class DestinationDialog(FormDialog):
    def __init__(
        self,
        dest: Destination | None,
        taken_names: set[str],
        global_template: str,
        on_save: Callable[[Destination], None],
    ) -> None:
        super().__init__(_("Edit Destination") if dest else _("New Destination"), on_save)
        self.dest = dataclasses.replace(dest) if dest else Destination(name="", path="")
        self.taken_names = taken_names

        group = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title=_("Name"), text=self.dest.name)
        self.name_row.connect("changed", self.check)
        group.add(self.name_row)

        self.folder_row = Adw.ActionRow(title=_("Folder"), use_markup=False)
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_folder)
        self.folder_row.add_suffix(choose)
        self.folder_row.set_activatable_widget(choose)
        group.add(self.folder_row)

        self.icon_row = Adw.ActionRow(title=_("Icon"))
        self.icon_button = Gtk.MenuButton(valign=Gtk.Align.CENTER, popover=self._icon_popover(), tooltip_text=_("Choose an icon"))
        self.icon_row.add_suffix(self.icon_button)
        self.icon_row.set_activatable_widget(self.icon_button)
        group.add(self.icon_row)
        self.page.add(group)

        ocr = Adw.PreferencesGroup(title=_("OCR"))
        self.table_layout = Adw.SwitchRow(
            title=_("Tables"),
            subtitle=_("Read tables row by row, e.g. for invoices (worse on text in columns)"),
            active=self.dest.table_layout,
        )
        ocr.add(self.table_layout)
        self.page.add(ocr)

        own = Adw.PreferencesGroup(title=_("File name"))
        self.own_template = Adw.SwitchRow(
            title=_("Own file name template"),
            subtitle=_("Instead of the template on the Naming page"),
            active=bool(self.dest.template),
        )
        own.add(self.own_template)
        self.page.add(own)
        self.template_editor = TemplateEditor(self.dest.template or global_template, self.dest.name or _("Documents"))
        self.template_editor.connect("template-changed", self.check)
        self.template_editor.entry.connect("changed", self.check)
        self.own_template.bind_property("active", self.template_editor, "sensitive", GObject.BindingFlags.SYNC_CREATE)
        self.own_template.connect("notify::active", self.check)
        self.page.add(self.template_editor)

        self._show_folder()
        self._show_icon()
        self.check()

    # --- folder & icon ---------------------------------------------------------

    def _show_folder(self) -> None:
        self.folder_row.set_subtitle(self.dest.path or _("No folder chosen"))

    def _choose_folder(self, _button) -> None:
        dialog = Gtk.FileDialog(title=_("Destination Folder"), modal=True)
        if self.dest.path:
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.dest.expanded_path())))
        dialog.select_folder(self.get_root(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:  # dismissed
            return
        if folder and folder.get_path():
            self.dest.path = contract_home(folder.get_path())
            self._show_folder()
            self.check()

    def _icon_popover(self) -> Gtk.Popover:
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6, min_children_per_line=6)
        for name in available_icons():
            button = Gtk.Button(child=icon_image(name), css_classes=["flat"], tooltip_text=name)
            button.connect("clicked", self._on_icon_picked, name)
            grid.append(button)
        from_file = Gtk.Button(label=_("From a File…"), css_classes=["flat"], margin_top=6)
        from_file.connect("clicked", self._choose_icon_file)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.append(grid)
        box.append(from_file)
        return Gtk.Popover(child=box)

    def _show_icon(self) -> None:
        self.icon_button.set_child(icon_image(self.dest.icon))

    def _on_icon_picked(self, _button, icon: str) -> None:
        self.dest.icon = icon
        self.icon_button.popdown()
        self._show_icon()

    def _choose_icon_file(self, _button) -> None:
        self.icon_button.popdown()
        images = Gtk.FileFilter(name=_("Images"))
        for mime in ("image/svg+xml", "image/png"):
            images.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog = Gtk.FileDialog(title=_("Icon"), modal=True, filters=filters, default_filter=images)
        dialog.open(self.get_root(), None, self._on_icon_file_chosen)

    def _on_icon_file_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file and file.get_path():
            self.dest.icon = file.get_path()
            self._show_icon()

    # --- validation ----------------------------------------------------------

    def validate(self) -> bool:
        name = self.name_row.get_text().strip()
        duplicate = name in self.taken_names
        mark_error(self.name_row, duplicate)
        self.name_row.set_tooltip_text(_("Another destination already has this name.") if duplicate else None)
        template_ok = not self.own_template.get_active() or self.template_editor.valid
        return bool(name) and not duplicate and bool(self.dest.path) and template_ok

    def result(self) -> Destination:
        self.dest.name = self.name_row.get_text().strip()
        self.dest.template = self.template_editor.template if self.own_template.get_active() else ""
        self.dest.table_layout = self.table_layout.get_active()
        return self.dest


class ScannerDialog(FormDialog):
    def __init__(self, scanner: Scanner | None, on_save: Callable[[Scanner], None]) -> None:
        super().__init__(_("Edit Scanner") if scanner else _("New Scanner"), on_save)
        self.scanner = dataclasses.replace(scanner) if scanner else Scanner(name="", vendor_id="")

        # "Plug your scanner now": USB disks appear here as they are plugged in.
        self.detect = Adw.PreferencesGroup(
            title=_("Detect"),
            description=_("Plug in your scanner now (or leave it plugged in), then click “Use”."),
        )
        self.waiting = Adw.ActionRow(title=_("Waiting for a USB storage device…"))
        self.waiting.add_prefix(Adw.Spinner())
        self.detect.add(self.waiting)
        self.page.add(self.detect)
        self._disk_rows: dict[str, Adw.ActionRow] = {}
        self.watcher = UDisksWatcher()
        self._watch = [
            self.watcher.connect("added", self._on_disk_added),
            self.watcher.connect("removed", self._on_disk_removed),
        ]
        self.connect("closed", self._stop_watching)
        self.watcher.start()

        ids = Adw.PreferencesGroup(
            title=_("Identification"),
            description=_(
                "Filled in by “Use” above, or by hand: the “lsusb” command shows "
                "“ID 07b4:0109” for vendor 07b4 and model 0109."
            ),
        )
        self.name_row = self._entry(ids, _("Name"), self.scanner.name)
        self.vendor_row = self._entry(ids, _("Vendor ID"), self.scanner.vendor_id)
        self.model_row = self._entry(ids, _("Model ID (empty: any model)"), self.scanner.model_id)
        self.serial_row = self._entry(ids, _("Serial number (empty: any unit)"), self.scanner.serial)
        self.page.add(ids)

        files = Adw.PreferencesGroup()
        self.source_row = self._entry(files, _("Folder on the scanner (empty: whole memory)"), self.scanner.source_dir)
        self.delete_row = Adw.SwitchRow(
            title=_("Delete from the scanner after import"),
            subtitle=_("Once the searchable PDFs have been written"),
            active=self.scanner.delete_after_import,
        )
        files.add(self.delete_row)
        self.page.add(files)
        self.check()

    # --- detection ---------------------------------------------------------------

    def _on_disk_added(self, _watcher, disk: UsbDisk) -> None:
        row = Adw.ActionRow(
            title=disk.label, subtitle=f"{disk.vendor_id}:{disk.model_id}  ·  {disk.device}", use_markup=False
        )
        row.add_prefix(Gtk.Image(icon_name="drive-removable-media-symbolic"))
        use = Gtk.Button(label=_("_Use"), use_underline=True, valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        use.connect("clicked", lambda _b: self.use_disk(disk))
        row.add_suffix(use)
        row.set_activatable_widget(use)
        self._disk_rows[disk.object_path] = row
        self.detect.add(row)
        self.waiting.set_visible(False)

    def _on_disk_removed(self, _watcher, object_path: str) -> None:
        row = self._disk_rows.pop(object_path, None)
        if row is not None:
            self.detect.remove(row)
        self.waiting.set_visible(not self._disk_rows)

    def use_disk(self, disk: UsbDisk) -> None:
        if not self.name_row.get_text().strip():
            self.name_row.set_text(disk.label)
        self.vendor_row.set_text(disk.vendor_id)
        self.model_row.set_text(disk.model_id)
        # Serial numbers are often shared by every unit of a model: not used by default.
        self.serial_row.set_text("")
        if not self.source_row.get_text().strip():
            for point in disk.mount_points:
                if os.path.isdir(os.path.join(point, "DCIM")):  # camera-style memory
                    self.source_row.set_text("DCIM")
                    break

    def _stop_watching(self, _dialog) -> None:
        for handler in self._watch:
            self.watcher.disconnect(handler)
        self._watch = []

    def _entry(self, group: Adw.PreferencesGroup, title: str, text: str) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title, text=text)
        row.connect("changed", self.check)
        group.add(row)
        return row

    def validate(self) -> bool:
        vendor = self.vendor_row.get_text().strip()
        model = self.model_row.get_text().strip()
        vendor_ok = config.is_usb_id(vendor)
        model_ok = not model or config.is_usb_id(model)
        mark_error(self.vendor_row, bool(vendor) and not vendor_ok)
        mark_error(self.model_row, not model_ok)
        return bool(self.name_row.get_text().strip()) and vendor_ok and model_ok

    def result(self) -> Scanner:
        s = self.scanner
        s.name = self.name_row.get_text().strip()
        s.vendor_id = self.vendor_row.get_text().strip().lower()
        s.model_id = self.model_row.get_text().strip().lower()
        s.serial = self.serial_row.get_text().strip()
        s.source_dir = self.source_row.get_text().strip().strip("/")
        s.delete_after_import = self.delete_row.get_active()
        return s
