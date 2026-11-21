"""Small helpers shared by the windows."""

from __future__ import annotations

import os

from gi.repository import Gdk, Gtk

from flypage.paths import contract_home  # noqa: F401  (used by the windows)

# Offered in the destination icon picker; names missing from the icon theme are skipped.
ICON_CHOICES = (
    "folder-symbolic",
    "folder-documents-symbolic",
    "folder-download-symbolic",
    "folder-pictures-symbolic",
    "folder-music-symbolic",
    "folder-videos-symbolic",
    "folder-templates-symbolic",
    "folder-publicshare-symbolic",
    "folder-remote-symbolic",
    "user-home-symbolic",
    "user-desktop-symbolic",
    "user-bookmarks-symbolic",
    "starred-symbolic",
    "x-office-document-symbolic",
    "x-office-spreadsheet-symbolic",
    "x-office-address-book-symbolic",
    "text-x-generic-symbolic",
    "mail-unread-symbolic",
    "accessories-dictionary-symbolic",
    "applications-science-symbolic",
    "system-users-symbolic",
    "camera-photo-symbolic",
    "image-x-generic-symbolic",
    "emoji-objects-symbolic",
)


def available_icons() -> list[str]:
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    return [name for name in ICON_CHOICES if theme.has_icon(name)]


def icon_image(icon: str, pixel_size: int = 24) -> Gtk.Image:
    """An image for a destination icon: a theme icon name, or the path of an image file."""
    if os.path.isabs(icon) and os.path.exists(icon):
        image = Gtk.Image.new_from_file(icon)
    else:
        image = Gtk.Image.new_from_icon_name(icon or "folder-symbolic")
    image.set_pixel_size(pixel_size)
    return image


def mark_error(widget: Gtk.Widget, error: bool) -> None:
    if error:
        widget.add_css_class("error")
    else:
        widget.remove_css_class("error")
