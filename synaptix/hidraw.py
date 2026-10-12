"""Linux hidraw access: device discovery and feature reports, no kernel driver needed.

Access to /dev/hidraw* needs a udev rule granting the logged-in user access
(see data/70-synaptix-razer.rules).
"""

import fcntl
import os
import threading
from dataclasses import dataclass, field

SYS_HIDRAW = "/sys/class/hidraw"
BUS_USB = 0x03


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


_IOC_READ_WRITE = 3


def hidiocsfeature(length: int) -> int:
    return _ioc(_IOC_READ_WRITE, 0x06, length)


def hidiocgfeature(length: int) -> int:
    return _ioc(_IOC_READ_WRITE, 0x07, length)


class PermissionDenied(Exception):
    pass


@dataclass
class HidInterface:
    number: int
    node: str
    # (usage page, usage) of each top-level application collection
    usages: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class UsbDevice:
    path: str
    vendor_id: int
    product_id: int
    product_name: str | None
    interfaces: list[HidInterface] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.path


def _read(path: str, binary: bool = False):
    try:
        with open(path, "rb" if binary else "r") as f:
            data = f.read()
        return data if binary else data.strip()
    except OSError:
        return None


def _parse_hid_id(uevent: str) -> tuple[int, int, int] | None:
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            bus, vendor, product = line[len("HID_ID="):].split(":")
            return int(bus, 16), int(vendor, 16), int(product, 16)
    return None


def parse_top_level_usages(descriptor: bytes) -> list[tuple[int, int]]:
    """Usage page and usage of each top-level application collection in a report descriptor."""
    usages = []
    usage_page = 0
    local_usages: list[tuple[int, bool]] = []  # (value, value includes its usage page)
    depth = 0
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item
            if i + 1 >= len(descriptor):
                break
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        item_type = (prefix >> 2) & 0x03
        tag = prefix >> 4
        value = int.from_bytes(descriptor[i + 1:i + 1 + size], "little")
        i += 1 + size
        if item_type == 1 and tag == 0x0:  # Usage Page
            usage_page = value
        elif item_type == 2 and tag == 0x0:  # Usage
            local_usages.append((value, size == 4))
        elif item_type == 0:  # main item
            if tag == 0xA:  # Collection
                if depth == 0 and value == 0x01 and local_usages:
                    usage, extended = local_usages[0]
                    usages.append((usage >> 16 if extended else usage_page, usage & 0xFFFF))
                depth += 1
            elif tag == 0xC:  # End Collection
                depth = max(0, depth - 1)
            local_usages = []
    return usages


def discover(vendor_id: int) -> list[UsbDevice]:
    found: dict[str, UsbDevice] = {}
    try:
        nodes = sorted(os.listdir(SYS_HIDRAW))
    except OSError:
        return []
    for node in nodes:
        hid_dir = os.path.realpath(os.path.join(SYS_HIDRAW, node, "device"))
        ids = _parse_hid_id(_read(os.path.join(hid_dir, "uevent")) or "")
        if ids is None or ids[0] != BUS_USB or ids[1] != vendor_id:
            continue
        interface_dir = os.path.dirname(hid_dir)
        number = _read(os.path.join(interface_dir, "bInterfaceNumber"))
        if number is None:
            continue
        usb_path = os.path.dirname(interface_dir)
        device = found.setdefault(usb_path, UsbDevice(
            usb_path, ids[1], ids[2], _read(os.path.join(usb_path, "product"))))
        descriptor = _read(os.path.join(hid_dir, "report_descriptor"), binary=True) or b""
        device.interfaces.append(HidInterface(int(number, 16), os.path.join("/dev", node),
                                              parse_top_level_usages(descriptor)))
    for device in found.values():
        device.interfaces.sort(key=lambda i: i.number)
    return list(found.values())


class Transport:
    """Feature reports on one hidraw node; safe to share between threads."""

    def __init__(self, node: str):
        self.node = node
        self.lock = threading.RLock()
        try:
            self._fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError as e:
            raise PermissionDenied(node) from e

    def close(self):
        with self.lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1

    def send_feature(self, report_id: int, data: bytes):
        buf = bytearray([report_id]) + bytearray(data)
        with self.lock:
            fcntl.ioctl(self._fd, hidiocsfeature(len(buf)), buf)

    def get_feature(self, report_id: int, length: int) -> bytes:
        buf = bytearray(length + 1)
        buf[0] = report_id
        with self.lock:
            fcntl.ioctl(self._fd, hidiocgfeature(len(buf)), buf, True)
        return bytes(buf[1:])
