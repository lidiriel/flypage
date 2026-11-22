"""``flypage daemon``: import a scanner's files when it is plugged in (PLAN.md §3).

Runs in the user session (XDG autostart), without GTK. For each plugged USB
disk matching a scanner profile: wait for the desktop to mount it (or mount
it), apply pending deletions, copy its files to a staging folder, then open
the processing window on them as a separate process.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from gi.repository import Gio, GLib

import flypage
from flypage import config, importer, notifications, pending
from flypage.config import Scanner
from flypage.device import UDisksWatcher, UsbDisk
from flypage.i18n import _
from flypage.scanners import match

log = logging.getLogger(__name__)

DAEMON_APP_ID = "io.github.flypage.Daemon"


@dataclass
class Job:
    disk: UsbDisk
    scanner: Scanner
    cancelled: threading.Event = field(default_factory=threading.Event)
    window: Gio.Subprocess | None = None


class Daemon(Gio.Application):
    def __init__(self) -> None:
        # Unique: starting a second daemon just activates the first one.
        super().__init__(application_id=DAEMON_APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.jobs: dict[str, Job] = {}  # by UDisks2 object path
        self.watcher = UDisksWatcher()

    def do_startup(self) -> None:
        Gio.Application.do_startup(self)
        self.hold()  # run until killed or logged out
        open_folder = Gio.SimpleAction.new("open-folder", GLib.VariantType.new("s"))
        open_folder.connect("activate", lambda _a, folder: notifications.open_folder(folder.get_string()))
        self.add_action(open_folder)
        self.watcher.connect("added", self._on_added)
        self.watcher.connect("removed", self._on_removed)
        self.watcher.start()
        log.info("watching for scanners")

    def do_activate(self) -> None:
        pass  # nothing to show

    # --- plug ------------------------------------------------------------------

    def _on_added(self, _watcher, disk: UsbDisk) -> None:
        scanner = match(config.load().scanners, disk)  # settings may have changed
        if scanner is None:
            log.debug("ignoring %s (%s:%s)", disk.label, disk.vendor_id, disk.model_id)
            return
        if disk.object_path in self.jobs:
            return
        log.info("%s plugged in (%s)", scanner.name, disk.device)
        job = Job(disk, scanner)
        self.jobs[disk.object_path] = job
        self.watcher.mount(disk.object_path, lambda point, error: self._on_mounted(job, point, error))

    def _on_mounted(self, job: Job, mount_point: str | None, error: str | None) -> None:
        if mount_point is None:
            self._fail(job, _("Cannot open the scanner memory: {error}").format(error=error))
            return
        self.notify(job, _("{scanner} detected").format(scanner=job.scanner.name), _("Copying its files…"))
        threading.Thread(target=self._import, args=(job, Path(mount_point)), daemon=True).start()

    def _import(self, job: Job, mount_point: Path) -> None:
        """Worker thread: pending deletions, then copy."""
        try:
            deleted = pending.apply(job.scanner.name, mount_point)
            staging = importer.import_files(mount_point, job.scanner, job.cancelled)
        except importer.ImportFailed as err:
            GLib.idle_add(self._fail, job, _("Import failed: {error}").format(error=err))
        except Exception as err:  # never let the daemon die on one bad device
            log.exception("import from %s failed", job.disk.device)
            GLib.idle_add(self._fail, job, _("Import failed: {error}").format(error=err))
        else:
            GLib.idle_add(self._on_imported, job, staging, deleted)

    def _on_imported(self, job: Job, staging: Path, deleted: int) -> bool:
        count = len(importer.Manifest.load(staging).entries)
        if deleted:
            log.info("%d pending deletion(s) done on %s", deleted, job.scanner.name)
        if count == 0:
            importer.ScannerFiles(staging).cleanup()
            self.notify(job, _("{scanner}: nothing to import").format(scanner=job.scanner.name),
                        _("The scanner memory has no new file."))  # fmt: skip
            self.jobs.pop(job.disk.object_path, None)
            return GLib.SOURCE_REMOVE
        log.info("%d file(s) imported from %s to %s", count, job.scanner.name, staging)
        self.withdraw_notification(job.disk.object_path)
        job.window = self._open_window(staging)
        job.window.communicate_utf8_async(None, None, self._on_window_closed, job)
        return GLib.SOURCE_REMOVE

    def _on_window_closed(self, process: Gio.Subprocess, result, job: Job) -> None:
        self.jobs.pop(job.disk.object_path, None)  # the next plug imports again
        try:
            _ok, stdout, _stderr = process.communicate_utf8_finish(result)
        except GLib.Error as err:
            log.warning("processing window: %s", err.message)
            stdout = ""
        report = notifications.parse_report(stdout or "")
        if not job.scanner.eject_when_closed:
            self._report(job, report, None)
            return
        # The window process has exited: nothing uses the scanner any more.
        self.watcher.eject(job.disk.object_path, lambda error: self._on_ejected(job, report, error))

    def _on_ejected(self, job: Job, report: dict | None, error: str | None) -> None:
        if error is None:
            log.info("%s ejected", job.scanner.name)
            line = _("You can unplug {scanner}.").format(scanner=job.scanner.name)
        else:
            log.warning("cannot eject %s: %s", job.scanner.name, error)
            line = _("{scanner} could not be ejected: {error}").format(scanner=job.scanner.name, error=error)
        self._report(job, report, line)

    def _report(self, job: Job, report: dict | None, eject_line: str | None) -> None:
        """The end notification: the files created, then the outcome of the eject if there was one."""
        if report and report.get("created"):
            folders = report.get("folders", [])
            notification = notifications.summary(report["created"], folders, "app.open-folder", report.get("pdfs"))
            if eject_line:
                notification.set_body(notifications.summary_body(folders) + "\n" + eject_line)
            self.send_notification(f"done-{job.disk.object_path}", notification)
        elif eject_line:
            self.notify(job, job.scanner.name, eject_line)

    def _open_window(self, staging: Path) -> Gio.Subprocess:
        # Its standard output carries the final report (flypage.notifications).
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE)
        # The window is the same flypage, even when installed outside the default Python path.
        package_root = str(Path(flypage.__file__).resolve().parent.parent)
        launcher.setenv("PYTHONPATH", os.pathsep.join(filter(None, [package_root, os.environ.get("PYTHONPATH")])), True)
        return launcher.spawnv([sys.executable, "-m", "flypage", "import-window", str(staging)])

    # --- unplug ----------------------------------------------------------------

    def _on_removed(self, _watcher, object_path: str) -> None:
        job = self.jobs.get(object_path)
        if job is not None and job.window is None:
            job.cancelled.set()  # copying: stop; the window keeps working from staging otherwise

    # --- helpers ---------------------------------------------------------------

    def _fail(self, job: Job, message: str) -> bool:
        log.warning("%s: %s", job.scanner.name, message)
        if not job.cancelled.is_set():  # an unplug during the copy is not worth a notification
            self.notify(job, job.scanner.name, message)
        self.jobs.pop(job.disk.object_path, None)
        return GLib.SOURCE_REMOVE

    def notify(self, job: Job, title: str, body: str) -> None:
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new("scanner-symbolic"))
        self.send_notification(job.disk.object_path, notification)


def run() -> int:
    return Daemon().run(sys.argv[:1])
