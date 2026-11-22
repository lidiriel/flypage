"""One row of destination icons; drop files on one to send them (PLAN.md §6.5)."""

from __future__ import annotations

from gi.repository import Gdk, GLib, GObject, Gtk, Pango

from flypage.config import Config, Destination
from flypage.i18n import _
from flypage.ui.thumbnail_grid import parse_payload
from flypage.ui.widgets import icon_image


class DestinationButton(Gtk.Button):
    def __init__(self, dest: Destination | None, label: str, icon: str, max_chars: int = 12) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(icon_image(icon, 48))
        box.append(Gtk.Label(label=label, ellipsize=Pango.EllipsizeMode.END, max_width_chars=max_chars))
        super().__init__(child=box, css_classes=["flat", "dest-button"])
        self.dest = dest
        self.label = label
        self.drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        self.add_controller(self.drop)


class DestinationBar(Gtk.ScrolledWindow):
    """Emits ``send(destination, ids)``; ``ids`` is None for a click (= current selection).

    The last button, *Other Folder…*, emits ``choose(ids)`` instead: the
    window asks for a folder, then sends.
    """

    __gtype_name__ = "FlypageDestinationBar"
    __gsignals__ = {
        "send": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "choose": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, cfg: Config) -> None:
        super().__init__(vscrollbar_policy=Gtk.PolicyType.NEVER, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.buttons = [DestinationButton(d, d.name, d.display_icon()) for d in cfg.destinations]
        for button in self.buttons:
            button.set_tooltip_text(
                _("{name}: {path}\nDrop files here, or click to send the selection.").format(
                    name=button.label, path=button.dest.path
                )
            )
            button.connect("clicked", lambda b: self.emit("send", b.dest, None))
            button.drop.connect("drop", self._on_drop, button)
            box.append(button)

        self.other = DestinationButton(None, _("Other Folder…"), "folder-open-symbolic", max_chars=18)
        self.other.set_tooltip_text(_("Choose any folder\nDrop files here, or click to send the selection."))
        self.other.connect("clicked", lambda _b: self.emit("choose", None))
        self.other.drop.connect("drop", self._on_drop_other)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=6, margin_end=6))
        box.append(self.other)
        self.set_child(box)

    def _on_drop(self, _target, value, _x, _y, button: DestinationButton) -> bool:
        ids = parse_payload(value)
        if ids is None:
            return False
        self.emit("send", button.dest, ids)
        return True

    def _on_drop_other(self, _target, value, _x, _y) -> bool:
        ids = parse_payload(value)
        if ids is None:
            return False
        # Ask once the drag has ended, not inside the drop handler.
        GLib.idle_add(lambda: self.emit("choose", ids) and False)
        return True
