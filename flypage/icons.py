"""The icon a folder has in the file manager (PLAN.md §9).

Nemo, Nautilus and Caja save a folder's custom icon as GIO metadata on the
folder: ``metadata::custom-icon-name`` (an icon of the theme) or
``metadata::custom-icon`` (the URI of an image file). Folders such as
Pictures or Downloads also have a standard icon of their own.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

FALLBACK = "folder-symbolic"
_GENERIC = {"inode-directory-symbolic", "inode-directory", "folder", "folder-symbolic"}
_ATTRIBUTES = "metadata::custom-icon,metadata::custom-icon-name,standard::symbolic-icon"


def folder_icon(path: Path) -> str:
    """The folder's icon: an icon name, or the path of an image file."""
    try:
        from gi.repository import Gio, GLib
    except ImportError:
        return FALLBACK
    try:
        info = Gio.File.new_for_path(str(path)).query_info(_ATTRIBUTES, Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error as err:  # missing folder, no GIO metadata store…
        log.debug("no icon for %s: %s", path, err)
        return FALLBACK

    uri = info.get_attribute_string("metadata::custom-icon")
    if uri:
        local = Gio.File.new_for_uri(uri).get_path()
        if local and Path(local).is_file():
            return local
    name = info.get_attribute_string("metadata::custom-icon-name")
    if name:
        return name
    # Special folders: folder-pictures-symbolic, folder-download-symbolic…
    icon = info.get_symbolic_icon()
    if isinstance(icon, Gio.ThemedIcon):
        for candidate in icon.get_names():
            if candidate not in _GENERIC:
                return candidate
    return FALLBACK
