"""Scanner import: matching, copy to staging, deletion on the scanner, pending deletions."""

import os
import threading

import pytest
from conftest import FakeEngine
from gi.repository import GLib
from test_session import wait_until

from flypage import importer, pending
from flypage.config import Config, Destination, Scanner
from flypage.device import BLOCK, DRIVE, FILESYSTEM, UDisksWatcher, UsbDisk, usb_disk
from flypage.importer import ImportFailed, ScannerFiles, import_files
from flypage.scanners import match
from flypage.session import Session

BOOK = Scanner(name="IRIScan Book", vendor_id="07b4", model_id="0109", source_dir="DCIM")


def disk(**kw):
    base = dict(object_path="/x/sdb1", device="/dev/sdb1", vendor_id="07b4", model_id="0109", serial="9645",
                vendor="OLYMPUS", model="FE210/X775")  # fmt: skip
    return UsbDisk(**{**base, **kw})


@pytest.fixture
def memory(tmp_path):
    """A scanner memory like the IRIScan Book's (see PLAN.md §3)."""
    root = tmp_path / "media" / "A94D-380E"
    (root / "DCIM" / "100MEDIA").mkdir(parents=True)
    (root / ".Trash-1000" / "files").mkdir(parents=True)
    (root / "System Volume Information").mkdir()
    for name, data in (("IMAG0001.JPG", b"jpeg-1"), ("IMAG0002.PDF", b"%PDF-2")):
        (root / "DCIM" / "100MEDIA" / name).write_bytes(data)
    (root / ".Trash-1000" / "files" / "old.JPG").write_bytes(b"old")
    (root / "System Volume Information" / "WPSettings.dat").write_bytes(b"x")
    return root


# --- matching ------------------------------------------------------------------


def test_match_by_vendor_model_and_serial():
    any_model = Scanner(name="Any", vendor_id="07b4")
    unit = Scanner(name="Unit", vendor_id="07b4", serial="1234")
    assert match([BOOK], disk()) is BOOK
    assert match([BOOK], disk(model_id="0281")) is None
    assert match([BOOK, any_model], disk(model_id="0281")) is any_model
    assert match([unit, BOOK], disk()) is BOOK
    assert match([BOOK], disk(vendor_id="1234")) is None


class FakeIface:
    def __init__(self, **props):
        self.props = props

    def get_cached_property(self, name):
        return self.props.get(name)


class FakeObject:
    def __init__(self, ifaces):
        self.ifaces = ifaces

    def get_interface(self, name):
        return self.ifaces.get(name)

    def get_object_path(self):
        return "/org/freedesktop/UDisks2/block_devices/sdb1"


def ay(text):
    return GLib.Variant("ay", text.encode() + b"\0")


def test_usb_disk_from_udisks_object():
    obj = FakeObject({
        BLOCK: FakeIface(Device=ay("/dev/sdb1")),
        FILESYSTEM: FakeIface(MountPoints=GLib.Variant("aay", [b"/media/me/A94D-380E\0"])),
    })  # fmt: skip
    udev = {"ID_BUS": "usb", "ID_VENDOR_ID": "07B4", "ID_MODEL_ID": "0109", "ID_SERIAL_SHORT": "9645",
            "ID_VENDOR": "OLYMPUS", "ID_MODEL": "FE210_X775"}  # fmt: skip
    found = usb_disk(obj, properties=lambda dev: udev if dev == "/dev/sdb1" else {})
    assert found == disk(object_path=obj.get_object_path(), serial="9645", model="FE210 X775",
                         mount_points=["/media/me/A94D-380E"])  # fmt: skip
    assert found.label == "OLYMPUS FE210 X775"


def test_non_usb_or_unformatted_disks_are_ignored():
    with_fs = {BLOCK: FakeIface(Device=ay("/dev/sda1")), FILESYSTEM: FakeIface()}
    assert usb_disk(FakeObject(with_fs), properties=lambda d: {"ID_BUS": "ata", "ID_VENDOR_ID": "1"}) is None
    no_fs = {BLOCK: FakeIface(Device=ay("/dev/sdb"))}
    assert usb_disk(FakeObject(no_fs), properties=lambda d: {"ID_BUS": "usb", "ID_VENDOR_ID": "1"}) is None


# --- eject ---------------------------------------------------------------------


class FakeCallIface(FakeIface):
    """Records D-Bus calls; ``fail`` maps a method to the error it raises; ``after`` runs on success."""

    def __init__(self, calls, fail=None, after=None, **props):
        super().__init__(**props)
        self.calls, self.fail, self.after = calls, fail or {}, after or {}

    def call(self, method, _params, _flags, _timeout, _cancellable, callback):
        self.calls.append(method)
        self._method = method
        callback(self, None)

    def call_finish(self, _result):
        if self._method in self.fail:
            raise GLib.Error(self.fail[self._method])
        self.after.get(self._method, lambda: None)()


class FakeManager:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, path):
        return self.objects.get(path)


def ejectable(mounted=True, can_power_off=True, fail=None):
    calls = []
    points = [b"/media/me/SCAN\0"] if mounted else []
    fs = FakeCallIface(calls, fail, MountPoints=GLib.Variant("aay", points))
    fs.after["Unmount"] = lambda: fs.props.update(MountPoints=GLib.Variant("aay", []))
    disk_path, drive_path = "/udisks/block_devices/sdb1", "/udisks/drives/scanner"
    drive = FakeCallIface(calls, fail, CanPowerOff=GLib.Variant("b", can_power_off))
    watcher = UDisksWatcher()
    watcher.manager = FakeManager({
        disk_path: FakeObject({BLOCK: FakeIface(Drive=GLib.Variant("o", drive_path)), FILESYSTEM: fs}),
        drive_path: FakeObject({DRIVE: drive}),
    })  # fmt: skip
    errors = []
    watcher.eject(disk_path, errors.append)
    return calls, errors


def test_eject_unmounts_then_powers_off():
    assert ejectable() == (["Unmount", "PowerOff"], [None])


def test_eject_of_an_unmounted_disk_only_powers_off():
    assert ejectable(mounted=False) == (["PowerOff"], [None])


def test_eject_uses_eject_when_power_off_is_not_possible():
    assert ejectable(can_power_off=False) == (["Unmount", "Eject"], [None])


def test_a_busy_disk_is_not_ejected():
    assert ejectable(fail={"Unmount": "target is busy"}) == (["Unmount"], ["target is busy"])


def test_a_failed_power_off_after_unmount_is_not_an_error():
    assert ejectable(fail={"PowerOff": "not authorized"}) == (["Unmount", "PowerOff"], [None])


def test_eject_of_a_gone_disk_does_nothing():
    watcher = UDisksWatcher()
    watcher.manager = FakeManager({})
    errors = []
    watcher.eject("/udisks/block_devices/sdb1", errors.append)
    assert errors == [None]


# --- import --------------------------------------------------------------------


def test_import_copies_only_the_source_folder(memory):
    staging = import_files(memory, BOOK)
    files = ScannerFiles(staging, is_mounted=lambda _p: True)
    assert [p.relative_to(staging).as_posix() for p in files.files()] == ["100MEDIA/IMAG0001.JPG", "100MEDIA/IMAG0002.PDF"]
    assert [e.relpath for e in files.manifest.entries] == ["DCIM/100MEDIA/IMAG0001.JPG", "DCIM/100MEDIA/IMAG0002.PDF"]
    assert files.scanner == "IRIScan Book" and files.delete_after_import
    assert (staging / "100MEDIA" / "IMAG0001.JPG").read_bytes() == b"jpeg-1"
    # Originals untouched by the import itself.
    assert (memory / "DCIM" / "100MEDIA" / "IMAG0001.JPG").exists()


def test_import_of_a_missing_folder_fails(memory):
    with pytest.raises(ImportFailed):
        import_files(memory, Scanner(name="X", vendor_id="07b4", source_dir="NOPE"))


def test_cancelled_import_leaves_nothing(memory):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ImportFailed):
        import_files(memory, BOOK, cancelled)
    assert list(importer.incoming_dir().iterdir()) == []


def test_unplug_during_copy_leaves_nothing(memory, monkeypatch):
    def unplugged(*_args, **_kw):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(importer.shutil, "copy2", unplugged)
    with pytest.raises(ImportFailed, match="Input/output"):
        import_files(memory, BOOK)
    assert list(importer.incoming_dir().iterdir()) == []


# --- deleting originals --------------------------------------------------------


def test_delete_original_when_mounted(memory):
    files = ScannerFiles(import_files(memory, BOOK), is_mounted=lambda _p: True)
    copy = files.files()[0]
    files.delete_original(copy)
    assert not (memory / "DCIM" / "100MEDIA" / "IMAG0001.JPG").exists()
    assert (memory / "DCIM" / "100MEDIA" / "IMAG0002.PDF").exists()
    assert pending.entries() == []


def test_a_changed_file_is_not_deleted(memory):
    files = ScannerFiles(import_files(memory, BOOK), is_mounted=lambda _p: True)
    original = memory / "DCIM" / "100MEDIA" / "IMAG0001.JPG"
    original.write_bytes(b"a new scan with the same name")
    files.delete_original(files.files()[0])
    assert original.exists()


def test_unplugged_deletion_is_postponed_then_applied(memory):
    files = ScannerFiles(import_files(memory, BOOK), is_mounted=lambda _p: False)
    files.delete_original(files.files()[1])
    assert (memory / "DCIM" / "100MEDIA" / "IMAG0002.PDF").exists()
    assert [e["relpath"] for e in pending.entries("IRIScan Book")] == ["DCIM/100MEDIA/IMAG0002.PDF"]

    assert pending.apply("Other scanner", memory) == 0
    assert pending.apply("IRIScan Book", memory) == 1
    assert not (memory / "DCIM" / "100MEDIA" / "IMAG0002.PDF").exists()
    assert pending.entries() == []


def test_pending_deletion_of_a_replaced_file_is_dropped(memory):
    original = memory / "DCIM" / "100MEDIA" / "IMAG0001.JPG"
    st = original.stat()
    pending.add("IRIScan Book", "DCIM/100MEDIA/IMAG0001.JPG", st.st_size, st.st_mtime_ns)
    os.utime(original, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))  # rescanned meanwhile
    assert pending.apply("IRIScan Book", memory) == 0
    assert original.exists()
    assert pending.entries() == []


def test_cleanup_removes_staging(memory):
    files = ScannerFiles(import_files(memory, BOOK))
    files.cleanup()
    assert not files.staging.exists()


# --- session with a scanner ----------------------------------------------------


@pytest.fixture
def scanner_session(memory, samples, tmp_path):
    # Real images on the "scanner", so the fake OCR can convert them.
    folder = memory / "DCIM" / "100MEDIA"
    for p in folder.iterdir():
        p.unlink()
    samples.png("IMAG0001.png").rename(folder / "IMAG0001.png")
    samples.jpeg("IMAG0002.jpg").rename(folder / "IMAG0002.jpg")
    originals = ScannerFiles(import_files(memory, BOOK), is_mounted=lambda _p: True)
    cfg = Config(destinations=[Destination(path=str(tmp_path / "out"))])
    return Session(cfg, originals.files(), engine=FakeEngine(), originals=originals), folder


def test_success_deletes_on_the_scanner(scanner_session):
    session, folder = scanner_session
    first = session.items[0]
    session.send([first], session.cfg.destinations[0])
    wait_until(lambda: not session.busy)
    assert not (folder / "IMAG0001.png").exists()
    assert (folder / "IMAG0002.jpg").exists()
    assert not first.path.exists()  # staging copy removed too


def test_no_deletion_on_the_scanner_when_the_profile_says_so(scanner_session):
    session, folder = scanner_session
    session.originals.manifest.delete_after_import = False
    session.send([session.items[0]], session.cfg.destinations[0])
    wait_until(lambda: not session.busy)
    assert (folder / "IMAG0001.png").exists()


def test_delete_button_deletes_on_the_scanner(scanner_session):
    session, folder = scanner_session
    session.originals.manifest.delete_after_import = False  # Delete deletes regardless
    assert session.delete([session.items[1]]) == []
    assert not (folder / "IMAG0002.jpg").exists()


def test_cancel_keeps_files_on_the_scanner_and_cleans_staging(scanner_session):
    session, folder = scanner_session
    staging = session.originals.staging
    session.cancel()
    assert not staging.exists()
    assert sorted(p.name for p in folder.iterdir()) == ["IMAG0001.png", "IMAG0002.jpg"]
