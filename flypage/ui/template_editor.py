"""File name template field: entry, clickable codes, and a live preview."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from flypage import naming
from flypage.i18n import N_, _
from flypage.ui.widgets import mark_error

# (code, description) — descriptions are translated when the editor is built.
CODES = (
    ("%Y", N_("year")),
    ("%m", N_("month")),
    ("%d", N_("day")),
    ("%H", N_("hour")),
    ("%M", N_("minute")),
    ("%S", N_("second")),
    ("%n", N_("number")),
    ("%3n", N_("number, 3 digits")),
    ("%{dest}", N_("destination")),
    ("%{original}", N_("original name")),
    ("%{scanner}", N_("scanner")),
    ("%{pages}", N_("pages")),
    ("%{scan_date}", N_("scan date")),
)


class TemplateEditor(Adw.PreferencesGroup):
    """Emits ``template-changed`` with the new text each time it becomes a valid template."""

    __gsignals__ = {"template-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, template: str, dest_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.dest_name = dest_name
        self.valid = False
        self._cursor_known = False

        self.entry = Adw.EntryRow(title=_("File name template"), text=template)
        self.entry.add_css_class("monospace")
        self.entry.connect("changed", self._on_changed)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", self._on_focus)
        self.entry.add_controller(focus)
        self.add(self.entry)

        self.preview = Adw.ActionRow(title=_("Example"), subtitle_selectable=True, use_markup=False)
        self.add(self.preview)

        codes = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=7,
            column_spacing=6,
            row_spacing=6,
            margin_top=12,
        )
        example = naming.example(dest_name)
        for code, description in CODES:
            codes.append(self._code_button(code, _(description), naming.render(code, example)))
        self.add(codes)

        self._on_changed(self.entry)

    def _code_button(self, code: str, description: str, example: str) -> Gtk.Button:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Gtk.Label(label=code, css_classes=["monospace", "heading"]))
        box.append(Gtk.Label(label=description, css_classes=["caption", "dim-label"]))
        button = Gtk.Button(child=box, tooltip_text=_("Insert {code} (e.g. “{example}”)").format(code=code, example=example))
        button.connect("clicked", lambda _button: self._insert(code))
        return button

    def _on_focus(self, _controller) -> None:
        self._cursor_known = True

    def _insert(self, code: str) -> None:
        # Until the user has clicked in the field, add at the end rather than
        # at position 0.
        position = self.entry.get_position() if self._cursor_known else len(self.entry.get_text())
        position = self.entry.insert_text(code, position)
        self.entry.grab_focus_without_selecting()
        self.entry.set_position(position)

    def _on_changed(self, entry: Adw.EntryRow) -> None:
        text = entry.get_text()
        try:
            name = naming.render(text, naming.example(self.dest_name))
        except naming.TemplateError as err:
            self.valid = False
            self.preview.set_title(_("Invalid template"))
            self.preview.set_subtitle(str(err))
        else:
            self.valid = True
            self.preview.set_title(_("Example"))
            self.preview.set_subtitle(name + ".pdf")
        mark_error(entry, not self.valid)
        mark_error(self.preview, not self.valid)
        if self.valid:
            self.emit("template-changed", text)

    @property
    def template(self) -> str:
        return self.entry.get_text()
