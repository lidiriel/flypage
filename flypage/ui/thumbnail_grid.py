"""Grid of scans with drag-and-drop and multi-selection (PLAN.md §6.3)."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from flypage import thumbnails
from flypage.i18n import _, ngettext
from flypage.model import FAILED, QUEUED, ImportedFile
from flypage.session import Session

log = logging.getLogger(__name__)

DRAG_PREFIX = "flypage-files:"  # drag content: "flypage-files:3,4,7"
CELL_SIZE = 168


DRAG_ICON_GAP = 12  # pixels between the pointer and the drag icon
_DRAG_ICON_SVG = Path(__file__).with_name("icons") / "drag-document.svg"
_BADGE_SVG = (
    '<circle cx="38" cy="11" r="9.5" fill="#e01b24" stroke="#ffffff" stroke-width="1.5"/>'
    '<text x="38" y="15" text-anchor="middle" font-family="sans-serif" font-size="{size}" '
    'font-weight="bold" fill="#ffffff">{count}</text>'
)
_drag_icons: dict[int, Gdk.Texture] = {}


def drag_icon(count: int) -> Gdk.Texture:
    """Document icon (Papirus "documentation") with a badge showing ``count`` when above 1."""
    if count not in _drag_icons:
        label = str(count) if count < 100 else "99+"
        badge = _BADGE_SVG.format(count=label, size=11 if len(label) == 1 else 9) if count > 1 else ""
        svg = _DRAG_ICON_SVG.read_text(encoding="utf-8").replace("{badge}", badge)
        svg = svg[svg.index("<svg") :]  # the loader sniffs the format from the first bytes
        _drag_icons[count] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(svg.encode("utf-8")))
    return _drag_icons[count]


def drag_payload(items: list[ImportedFile]) -> str:
    return DRAG_PREFIX + ",".join(str(i.id) for i in items)


def parse_payload(value) -> list[int] | None:
    """Ids of a drag started in the grid, or None for anything else (e.g. text from another app)."""
    if not isinstance(value, str) or not value.startswith(DRAG_PREFIX):
        return None
    try:
        return [int(x) for x in value[len(DRAG_PREFIX) :].split(",") if x]
    except ValueError:
        return None


class Thumbnailer:
    """Renders thumbnails in worker threads; hands Gdk textures back on the main thread."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="flypage-thumb")
        self._textures: dict[tuple[Path, int], Gdk.Texture | None] = {}
        self._waiting: dict[tuple[Path, int], list[Callable]] = defaultdict(list)

    def get(self, path: Path, size: int, callback: Callable[[Gdk.Texture | None], None]) -> None:
        """Call ``callback(texture)`` now if cached, else once rendered (None on failure)."""
        key = (path, size)
        if key in self._textures:
            callback(self._textures[key])
            return
        first = key not in self._waiting
        self._waiting[key].append(callback)
        if first:
            future = self._pool.submit(thumbnails.render, path, size)
            future.add_done_callback(lambda f: GLib.idle_add(self._done, key, f))

    def _done(self, key, future) -> bool:
        try:
            texture = Gdk.Texture.new_from_filename(str(future.result()))
        except Exception as err:  # unreadable file: the cell shows a generic icon
            log.info("no thumbnail for %s: %s", key[0], err)
            texture = None
        if key[1] == thumbnails.THUMBNAIL:  # previews are big: only thumbnails stay in memory
            self._textures[key] = texture
        for callback in self._waiting.pop(key, []):
            callback(texture)
        return GLib.SOURCE_REMOVE

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class ProgressRing(Gtk.Overlay):
    """A queued file's estimated OCR progress: a ring with the percentage inside."""

    SIZE = 56
    LINE = 5

    def __init__(self) -> None:
        super().__init__(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.fraction = 0.0
        self.area = Gtk.DrawingArea(content_width=self.SIZE, content_height=self.SIZE)
        self.area.set_draw_func(self._draw)
        self.set_child(self.area)
        self.label = Gtk.Label(css_classes=["ring-label"], halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.add_overlay(self.label)

    def set_fraction(self, fraction: float) -> None:
        self.fraction = max(0.0, min(1.0, fraction))
        self.label.set_label(_("{percent} %").format(percent=int(self.fraction * 100)))
        self.area.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        x, y = width / 2, height / 2
        radius = min(width, height) / 2 - self.LINE / 2
        cr.set_source_rgba(0, 0, 0, 0.6)  # dark disc: readable over any scan
        cr.arc(x, y, radius + self.LINE / 2, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(self.LINE)
        cr.set_source_rgba(1, 1, 1, 0.25)
        cr.arc(x, y, radius - 1, 0, 2 * math.pi)
        cr.stroke()
        if self.fraction > 0:
            cr.set_source_rgba(0.21, 0.52, 0.89, 1)  # libadwaita blue
            start = -math.pi / 2
            cr.arc(x, y, radius - 1, start, start + 2 * math.pi * self.fraction)
            cr.stroke()


class ThumbnailCell(Gtk.Box):
    def __init__(self, grid: ThumbnailGrid) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["thumb-cell"])
        self.grid = grid
        self.item: ImportedFile | None = None
        self._handlers: list[int] = []

        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.set_size_request(CELL_SIZE, CELL_SIZE)
        overlay = Gtk.Overlay(child=self.picture)
        self.spinner = Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32, height_request=32)
        self.dest_badge = Gtk.Image(halign=Gtk.Align.END, valign=Gtk.Align.START, pixel_size=24, css_classes=["osd", "badge"])
        self.pages_badge = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.END, css_classes=["osd", "badge", "caption"])
        self.error_badge = Gtk.Image(
            icon_name="dialog-error-symbolic", halign=Gtk.Align.START, valign=Gtk.Align.END,
            pixel_size=24, css_classes=["error-badge"],
        )  # fmt: skip
        self.ring = ProgressRing()
        self.waiting = Gtk.Label(
            label=_("Waiting"), halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, css_classes=["osd", "badge", "caption"]
        )
        for widget in (self.spinner, self.ring, self.waiting, self.dest_badge, self.pages_badge, self.error_badge):
            overlay.add_overlay(widget)
        self.append(overlay)
        self.label = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=18, css_classes=["caption"])
        self.append(self.label)

        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_drag_prepare)
        self.add_controller(drag)

        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        menu_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu_click.connect("pressed", self._on_menu_click)
        self.add_controller(menu_click)

    # --- binding ---------------------------------------------------------------

    def bind(self, item: ImportedFile) -> None:
        self.item = item
        self.label.set_label(item.name)
        self.set_tooltip_text(item.name)
        self.picture.set_paintable(None)
        self.grid.thumbnailer.get(item.path, thumbnails.THUMBNAIL, lambda t, it=item: self._set_texture(it, t))
        for prop in ("state", "error", "dest-icon", "progress"):
            self._handlers.append(item.connect(f"notify::{prop}", lambda *_args: self._update()))
        self._update()

    def unbind(self) -> None:
        if self.item is not None:
            for handler in self._handlers:
                self.item.disconnect(handler)
        self._handlers = []
        self.item = None

    def _set_texture(self, item: ImportedFile, texture: Gdk.Texture | None) -> None:
        if item is not self.item:  # the cell was recycled meanwhile
            return
        if texture is None:
            texture = Gtk.IconTheme.get_for_display(self.get_display()).lookup_icon(
                "image-missing-symbolic", None, 64, 1, Gtk.TextDirection.NONE, 0
            )
        self.picture.set_paintable(texture)
        self._update()

    def _update(self) -> None:
        item = self.item
        if item is None:
            return
        queued = item.state == QUEUED
        self.spinner.set_visible(not queued and self.picture.get_paintable() is None)
        self.picture.set_opacity(0.35 if queued else 1.0)
        running = queued and item.progress >= 0
        self.ring.set_visible(running)
        if running:
            self.ring.set_fraction(item.progress)
        self.waiting.set_visible(queued and not running)

        self.dest_badge.set_visible(queued and bool(item.dest_icon))
        if queued:
            self.dest_badge.set_from_icon_name(item.dest_icon or "folder-symbolic")

        self.pages_badge.set_visible(item.pages > 1)
        self.pages_badge.set_label(ngettext("{n} page", "{n} pages", item.pages).format(n=item.pages))

        failed = item.state == FAILED
        self.error_badge.set_visible(failed)
        self.error_badge.set_tooltip_text(item.error if failed else None)
        self.set_tooltip_text(f"{item.name}\n{item.error}" if failed else item.name)

    # --- drag and drop -----------------------------------------------------------

    def _on_drag_prepare(self, source: Gtk.DragSource, _x, _y):
        if self.item is None or not self.item.sortable:
            return None
        items = self.grid.drag_set(self.item)
        # A small document icon beside the pointer (a thumbnail would hide the
        # folder icons it is dropped on). Negative hotspot: the icon sits
        # below-right of the pointer, leaving the drop target visible.
        source.set_icon(drag_icon(len(items)), -DRAG_ICON_GAP, -DRAG_ICON_GAP)
        return Gdk.ContentProvider.new_for_value(GObject.Value(GObject.TYPE_STRING, drag_payload(items)))

    def _on_drop(self, _target, value, _x, _y) -> bool:
        ids = parse_payload(value)
        if ids is None or self.item is None:
            return False
        self.grid.session.move(self.grid.session.by_id(ids), before=self.item)
        return True

    def _on_menu_click(self, gesture: Gtk.GestureClick, _n, x: float, y: float) -> None:
        if self.item is not None:
            self.grid.show_menu(self, self.item, x, y)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)


class ThumbnailGrid(Gtk.ScrolledWindow):
    __gtype_name__ = "FlypageThumbnailGrid"
    __gsignals__ = {
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "item-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, session: Session) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=True)
        self.session = session
        self.thumbnailer = Thumbnailer()
        self.selection = Gtk.MultiSelection.new(session.store)
        self.selection.connect("selection-changed", lambda *_args: self.emit("selection-changed"))

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, list_item: list_item.set_child(ThumbnailCell(self)))
        factory.connect("bind", self._on_bind)
        factory.connect("unbind", lambda _f, list_item: list_item.get_child().unbind())
        self.view = Gtk.GridView(
            model=self.selection, factory=factory, max_columns=12, min_columns=1, enable_rubberband=True,
            css_classes=["thumb-grid"],
        )  # fmt: skip
        self.view.connect("activate", lambda _view, position: self.emit("item-activated", session.store.get_item(position)))
        self.set_child(self.view)

        # Dropping on the empty area after the last file moves files to the end.
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("drop", self._on_drop_at_end)
        self.add_controller(drop)

        self.menu = Gtk.PopoverMenu(has_arrow=False, halign=Gtk.Align.START)
        self.menu.set_parent(self)

        for item in session.store:  # bring any session state (e.g. failures) into view
            item.notify("state")
        session.store.connect("items-changed", self._on_items_changed)

    def _on_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        item: ImportedFile = list_item.get_item()
        list_item.set_selectable(item.sortable)
        list_item.get_child().bind(item)
        # Queued files cannot be selected (and are unselected as they get queued).
        handler = item.connect("notify::state", lambda it, _p, li=list_item: li.set_selectable(it.sortable))
        list_item.get_child()._handlers.append(handler)

    def _on_items_changed(self, *_args) -> None:
        self.emit("selection-changed")

    def selected(self) -> list[ImportedFile]:
        """Selected files that can be acted on, in grid order."""
        bitset = self.selection.get_selection()
        items = [self.session.store.get_item(bitset.get_nth(i)) for i in range(bitset.get_size())]
        return [i for i in items if i is not None and i.sortable]

    def drag_set(self, item: ImportedFile) -> list[ImportedFile]:
        """Files dragged from ``item``: the selection if it contains it, else ``item`` alone."""
        selected = self.selected()
        return selected if item in selected else [item]

    def select(self, items: list[ImportedFile]) -> None:
        """Select exactly ``items``."""
        self.selection.unselect_all()
        for item in items:
            found, position = self.session.store.find(item)
            if found:
                self.selection.select_item(position, False)

    def unselect(self, items: list[ImportedFile]) -> None:
        for item in items:
            found, position = self.session.store.find(item)
            if found:
                self.selection.unselect_item(position)

    def show_menu(self, cell: ThumbnailCell, item: ImportedFile, x: float, y: float) -> None:
        if not item.sortable:
            return
        menu = Gio.Menu()
        menu.append(_("Delete"), "win.delete")
        found, position = self.session.store.find(item)
        if found and not self.selection.is_selected(position):
            self.selection.select_item(position, True)  # the menu acts on what was right-clicked
        ok, point = cell.compute_point(self, Graphene.Point().init(x, y))
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(point.x), int(point.y), 1, 1
        self.menu.set_menu_model(menu)
        self.menu.set_pointing_to(rect)
        self.menu.popup()

    def _on_drop_at_end(self, _target, value, _x, _y) -> bool:
        ids = parse_payload(value)
        if ids is None:
            return False
        self.session.move(self.session.by_id(ids), before=None)
        return True

    def do_unrealize(self) -> None:
        self.thumbnailer.shutdown()
        Gtk.ScrolledWindow.do_unrealize(self)

    def do_dispose(self) -> None:
        if self.menu.get_parent() is not None:
            self.menu.unparent()
        Gtk.ScrolledWindow.do_dispose(self)

