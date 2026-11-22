"""USB disks as the system sees them: UDisks2 (D-Bus) for devices and mounts, udev for USB IDs.

No root needed: UDisks2 lets the user of the active session mount removable
media, and udev properties are readable by anyone.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from gi.repository import Gio, GLib, GObject

log = logging.getLogger(__name__)

UDISKS = "org.freedesktop.UDisks2"
UDISKS_PATH = "/org/freedesktop/UDisks2"
BLOCK = "org.freedesktop.UDisks2.Block"
FILESYSTEM = "org.freedesktop.UDisks2.Filesystem"
DRIVE = "org.freedesktop.UDisks2.Drive"


@dataclass
class UsbDisk:
    """A mountable partition of a USB disk."""

    object_path: str  # UDisks2 object, e.g. /org/freedesktop/UDisks2/block_devices/sdb1
    device: str  # /dev/sdb1
    vendor_id: str  # "07b4"
    model_id: str  # "0109"
    serial: str
    vendor: str  # "OLYMPUS"
    model: str  # "FE210/X775"
    mount_points: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.vendor} {self.model}".strip() or self.device


def udev_properties(device: str) -> dict[str, str]:
    try:
        out = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={device}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError) as err:
        log.debug("no udev properties for %s: %s", device, err)
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def _bytestring(variant: GLib.Variant | None) -> str:
    """UDisks2 paths are NUL-terminated byte arrays ('ay')."""
    return bytes(variant.unpack()).rstrip(b"\0").decode(errors="replace") if variant is not None else ""


def mount_points(obj: Gio.DBusObject) -> list[str]:
    fs = obj.get_interface(FILESYSTEM)
    if fs is None:
        return []
    value = fs.get_cached_property("MountPoints")
    return [bytes(m).rstrip(b"\0").decode(errors="replace") for m in value.unpack()] if value is not None else []


def usb_disk(obj: Gio.DBusObject, properties: Callable[[str], dict[str, str]] = udev_properties) -> UsbDisk | None:
    """The partition behind ``obj`` if it is a USB disk with a filesystem, else None."""
    block = obj.get_interface(BLOCK)
    if block is None or obj.get_interface(FILESYSTEM) is None:
        return None
    device = _bytestring(block.get_cached_property("Device"))
    props = properties(device)
    if props.get("ID_BUS") != "usb" or "ID_VENDOR_ID" not in props:
        return None
    return UsbDisk(
        object_path=obj.get_object_path(),
        device=device,
        vendor_id=props.get("ID_VENDOR_ID", "").lower(),
        model_id=props.get("ID_MODEL_ID", "").lower(),
        serial=props.get("ID_SERIAL_SHORT", ""),
        vendor=props.get("ID_VENDOR", "").replace("_", " "),
        model=props.get("ID_MODEL", "").replace("_", " "),
        mount_points=mount_points(obj),
    )


class UDisksWatcher(GObject.Object):
    """Reports USB disks: ``added`` (UsbDisk) when one appears or is present at start, ``removed`` (object path)."""

    __gtype_name__ = "FlypageUDisksWatcher"
    __gsignals__ = {
        "added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "removed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "ready": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.manager: Gio.DBusObjectManagerClient | None = None
        self.disks: dict[str, UsbDisk] = {}

    def start(self) -> None:
        """Connect to UDisks2 (asynchronously); raises nothing, logs failures."""
        Gio.DBusObjectManagerClient.new_for_bus(
            Gio.BusType.SYSTEM, Gio.DBusObjectManagerClientFlags.NONE, UDISKS, UDISKS_PATH,
            get_proxy_type_func=None, cancellable=None, callback=self._on_manager,
        )  # fmt: skip

    def _on_manager(self, _source, result) -> None:
        try:
            self.manager = Gio.DBusObjectManagerClient.new_for_bus_finish(result)
        except GLib.Error as err:
            log.warning("UDisks2 is not available: %s", err.message)
            return
        self.manager.connect("object-added", lambda _m, obj: self._check(obj))
        self.manager.connect("interface-added", lambda _m, obj, _iface: self._check(obj))
        self.manager.connect("object-removed", self._on_removed)
        for obj in self.manager.get_objects():
            self._check(obj)
        self.emit("ready")

    def _check(self, obj: Gio.DBusObject) -> None:
        path = obj.get_object_path()
        if path in self.disks:
            return
        disk = usb_disk(obj)
        if disk is not None:
            self.disks[path] = disk
            self.emit("added", disk)

    def _on_removed(self, _manager, obj: Gio.DBusObject) -> None:
        if self.disks.pop(obj.get_object_path(), None) is not None:
            self.emit("removed", obj.get_object_path())

    def current_mount_points(self, object_path: str) -> list[str]:
        obj = self.manager.get_object(object_path) if self.manager else None
        return mount_points(obj) if obj is not None else []

    def mount(self, object_path: str, callback: Callable[[str | None, str | None], None], wait_s: float = 4) -> None:
        """Call ``callback(mount_point, error)`` once the disk is mounted.

        The desktop usually mounts removable media itself a moment after it
        appears: wait up to ``wait_s`` for that, then mount it ourselves.
        """
        waited = 0.0

        def poll() -> bool:
            nonlocal waited
            points = self.current_mount_points(object_path)
            if points:
                callback(points[0], None)
                return GLib.SOURCE_REMOVE
            waited += 0.25
            if waited < wait_s:
                return GLib.SOURCE_CONTINUE
            self._mount_now(object_path, callback)
            return GLib.SOURCE_REMOVE

        if poll() is GLib.SOURCE_CONTINUE:
            GLib.timeout_add(250, poll)

    def _mount_now(self, object_path: str, callback) -> None:
        obj = self.manager.get_object(object_path) if self.manager else None
        fs = obj.get_interface(FILESYSTEM) if obj is not None else None
        if fs is None:
            callback(None, "the device is gone")
            return

        def done(proxy, result) -> None:
            try:
                (point,) = proxy.call_finish(result).unpack()
                callback(point, None)
            except GLib.Error as err:
                points = self.current_mount_points(object_path)  # mounted meanwhile by the desktop?
                callback(points[0], None) if points else callback(None, err.message)

        fs.call("Mount", GLib.Variant("(a{sv})", ({},)), Gio.DBusCallFlags.NONE, 30_000, None, done)

    def eject(self, object_path: str, callback: Callable[[str | None], None]) -> None:
        """Unmount the disk, then power off its drive, as *Safely Remove* does; ``callback(error)``.

        ``error`` is None once done, or when the disk is already gone.
        """
        obj = self.manager.get_object(object_path) if self.manager else None
        fs = obj.get_interface(FILESYSTEM) if obj is not None else None
        if fs is None:
            callback(None)
            return
        options = GLib.Variant("(a{sv})", ({},))

        def unmounted(proxy, result) -> None:
            try:
                proxy.call_finish(result)
            except GLib.Error as err:
                if self.current_mount_points(object_path):  # still mounted: busy, refused…
                    callback(err.message)
                    return
            self._power_off(obj, callback)

        if mount_points(obj):
            fs.call("Unmount", options, Gio.DBusCallFlags.NONE, 30_000, None, unmounted)
        else:
            self._power_off(obj, callback)

    def _power_off(self, obj: Gio.DBusObject, callback: Callable[[str | None], None]) -> None:
        block = obj.get_interface(BLOCK)
        drive_path = block.get_cached_property("Drive").unpack() if block is not None else "/"
        drive_obj = self.manager.get_object(drive_path) if drive_path != "/" else None
        drive = drive_obj.get_interface(DRIVE) if drive_obj is not None else None
        if drive is None:
            callback(None)  # unmounted: safe to unplug
            return
        can = drive.get_cached_property("CanPowerOff")
        method = "PowerOff" if can is not None and can.unpack() else "Eject"

        def done(proxy, result) -> None:
            try:
                proxy.call_finish(result)
            except GLib.Error as err:
                # Unmounted already, so unplugging is safe even if the drive stays powered.
                log.info("%s of %s failed: %s", method, obj.get_object_path(), err.message)
            callback(None)

        drive.call(method, GLib.Variant("(a{sv})", ({},)), Gio.DBusCallFlags.NONE, 30_000, None, done)
