"""Which configured scanner profile a plugged USB disk is."""

from __future__ import annotations

from flypage.config import Scanner
from flypage.device import UsbDisk


def match(scanners: list[Scanner], disk: UsbDisk) -> Scanner | None:
    """The first profile whose IDs fit ``disk`` (empty model/serial = any)."""
    for scanner in scanners:
        if scanner.vendor_id != disk.vendor_id:
            continue
        if scanner.model_id and scanner.model_id != disk.model_id:
            continue
        if scanner.serial and scanner.serial != disk.serial:
            continue
        return scanner
    return None
