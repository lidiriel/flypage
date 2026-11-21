"""Bring a newly opened window to the front with the keyboard focus.

The window is not kept above others: once shown, it behaves like any window.
A window opened by the daemon (not by a click) is normally held back by the
window manager's focus-stealing prevention. On X11 we ask the window manager
to activate it the way a taskbar does (EWMH ``_NET_ACTIVE_WINDOW`` with
source 2), which Muffin, Mutter, Xfwm and KWin honour. Elsewhere (Wayland),
``present()`` is all an application may do.
"""

from __future__ import annotations

import logging

from gi.repository import Gtk

log = logging.getLogger(__name__)

_SOURCE_PAGER = 2  # EWMH: request from a pager/taskbar, i.e. on the user's behalf


def bring_to_front(window: Gtk.Window) -> None:
    window.present()
    xid = _x11_window_id(window)
    if xid is None:
        return
    try:
        from Xlib import X, protocol
        from Xlib import display as xdisplay
    except ImportError:
        log.debug("python-xlib missing: the window may open behind others")
        return
    try:
        conn = xdisplay.Display()
        try:
            root = conn.screen().root
            event = protocol.event.ClientMessage(
                window=xid,
                client_type=conn.intern_atom("_NET_ACTIVE_WINDOW"),
                data=(32, [_SOURCE_PAGER, X.CurrentTime, 0, 0, 0]),
            )
            root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            conn.flush()
        finally:
            conn.close()
    except Exception as err:  # never fail to open the window because of this
        log.debug("cannot activate the window: %s", err)


def _x11_window_id(window: Gtk.Window) -> int | None:
    try:
        import gi

        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11
    except (ImportError, ValueError):
        return None
    surface = window.get_surface()
    if not isinstance(surface, GdkX11.X11Surface):
        return None
    return surface.get_xid()


def bring_to_front_when_shown(window: Gtk.Window) -> None:
    """Call ``bring_to_front`` once, as soon as the window is on screen."""

    def on_map(_window) -> None:
        window.disconnect(handler)
        bring_to_front(window)

    handler = window.connect("map", on_map)
