"""GTK applications. Kept apart from the command line so ``--version`` does not load GTK."""

from __future__ import annotations

import signal
import sys

import flypage.ui  # noqa: F401  (selects GTK 4 / libadwaita before the imports below)
from gi.repository import Adw, Gio, GLib

SETTINGS_APP_ID = "io.github.flypage.Settings"
PROCESSING_APP_ID = "io.github.flypage.Processing"
ICON_NAME = "io.github.flypage"


def _setup_icons() -> None:
    """Window icon, and fallbacks for icons some themes lack.

    Search paths are looked at after the user's icon theme: a theme that has
    the icon still wins. ui/icons/hicolor holds the few symbolic icons
    libadwaita or Flypage use that themes not inheriting from Adwaita (MATE,
    …) miss, such as the pencil of entry rows. They sit in symbolic/apps, the
    only symbolic folder hicolor's index.theme lists, so GTK recolours them.
    """
    from pathlib import Path

    from gi.repository import Gdk, Gtk

    from flypage import defs

    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    theme.add_search_path(str(Path(__file__).parent / "ui" / "icons"))
    source_icons = Path(defs.PKGDATADIR) / "icons"
    if source_icons.is_dir():  # not installed: the icon is not in the system theme
        theme.add_search_path(str(source_icons))
    Gtk.Window.set_default_icon_name(ICON_NAME)


def _run(app: Adw.Application) -> int:
    # The program name becomes the X11 WM_CLASS: the taskbar then finds the
    # app's .desktop file (same name as the app id) and its icon.
    GLib.set_prgname(app.get_application_id())
    app.connect("startup", lambda _app: _setup_icons())
    # GTK must not see Flypage's own command-line arguments.
    return app.run(sys.argv[:1])


class SettingsApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=SETTINGS_APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self) -> None:
        from flypage.ui.settings_window import SettingsWindow

        # Single instance: a second launch brings the open window to the front.
        window = self.get_active_window() or SettingsWindow(application=self)
        window.present()


class ProcessingApplication(Adw.Application):
    """One processing window per run; several can be open at once (e.g. two scanners)."""

    def __init__(self, files: list, source: str, remove_sources: bool, originals=None) -> None:
        super().__init__(application_id=PROCESSING_APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        # Started by the daemon: it shows the final notification (see flypage.notifications).
        self.report_to_daemon = originals is not None
        self.files = files
        self.source = source
        self.remove_sources = remove_sources
        self.originals = originals

    def do_activate(self) -> None:
        from flypage import config
        from flypage.session import Session
        from flypage.ui.processing_window import ProcessingWindow

        session = Session(
            config.load(), self.files, remove_sources=self.remove_sources, originals=self.originals
        )
        window = ProcessingWindow(session, self.source, application=self)
        window.present()
        # Logout or kill: stop OCR and remove the staging copies (the scanner keeps its files).
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_sigterm, window)

    def _on_sigterm(self, window) -> bool:
        window.session.cancel()
        window.report()
        self.quit()
        return GLib.SOURCE_REMOVE


def run_processing(files: list, source: str, remove_sources: bool, originals=None) -> int:
    return _run(ProcessingApplication(files, source, remove_sources, originals))


def run_settings() -> int:
    return _run(SettingsApplication())
