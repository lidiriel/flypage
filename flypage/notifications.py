"""End-of-session notification, and the report a processing window sends to the daemon.

A window started by the daemon prints one JSON line on its standard output
(``REPORT_PREFIX`` + JSON) when its session ends; the daemon, which keeps
running, shows the notification. Its *Open* buttons then still work after
the window has closed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from gi.repository import Gio, GLib

from flypage.i18n import _, ngettext
from flypage.paths import contract_home

log = logging.getLogger(__name__)

REPORT_PREFIX = "flypage-report: "
MAX_BUTTONS = 3  # notification servers show few buttons


def report_line(outputs: list[Path]) -> str:
    folders = list(dict.fromkeys(str(p.parent) for p in outputs))  # unique, in order
    return REPORT_PREFIX + json.dumps({"created": len(outputs), "folders": folders})


def parse_report(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(REPORT_PREFIX):
            try:
                return json.loads(line[len(REPORT_PREFIX) :])
            except ValueError:
                return None
    return None


def summary(created: int, folders: list[str], open_action: str | None = None) -> Gio.Notification:
    """The "N searchable PDFs created" notification; ``open_action`` adds one *Open* button per folder."""
    notification = Gio.Notification.new(
        ngettext("{n} searchable PDF created", "{n} searchable PDFs created", created).format(n=created)
    )
    notification.set_body(", ".join(contract_home(f) for f in folders))
    notification.set_icon(Gio.ThemedIcon.new("scanner-symbolic"))
    if open_action and folders:
        notification.set_default_action_and_target(open_action, GLib.Variant.new_string(folders[0]))
        for folder in folders[:MAX_BUTTONS]:
            notification.add_button_with_target(
                _("Open {folder}").format(folder=Path(folder).name), open_action, GLib.Variant.new_string(folder)
            )
    return notification


def open_folder(folder: str) -> None:
    try:
        Gio.AppInfo.launch_default_for_uri(Path(folder).as_uri(), None)
    except GLib.Error as err:
        log.warning("cannot open %s: %s", folder, err.message)
