"""Finds Razer devices, opens them and runs their commands on worker threads."""

import dataclasses
import errno
import logging
import os
import threading
import time

from gi.repository import GLib, GObject

from . import hidraw
from .razer import catalog
from .razer import protocol as p
from .razer.catalog import DeviceInfo
from .razer.driver import RazerDriver

log = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 2
KEEPALIVE_IDLE_SECONDS = 2.5
_PROBE_TRANSACTION_IDS = (0x1F, 0x3F, 0xFF, 0x9F)
_DISCONNECTED = (errno.ENODEV, errno.ENOENT, errno.EIO, errno.ENXIO, errno.EBADF)

# Simulated devices, for screenshots and development without hardware
DEMO = bool(os.environ.get("SYNAPTIX_DEMO"))


class Device(GObject.Object):
    """A device ready to receive lighting commands."""

    def __init__(self, key: str, driver: RazerDriver, worker: "_Worker", via: str | None = None):
        super().__init__()
        self.key = key
        self.driver = driver
        self.info: DeviceInfo = driver.info
        self.via = via
        self.firmware: str | None = None
        self.serial: str | None = None
        self.effects: list[str] = []
        self.supports_brightness = False
        self._worker = worker
        self.last_command = time.monotonic()

    @property
    def settings_key(self) -> str:
        return f"{catalog.RAZER_VENDOR_ID:04x}:{self.info.product_id:04x}"

    @property
    def name(self) -> str:
        return f"{self.info.name} ({self.via})" if self.via else self.info.name

    def run(self, kind: str, command):
        """Queues command(driver); a newer command of the same kind replaces a pending one."""
        self.last_command = time.monotonic()
        self._worker.submit((self.key, kind), lambda: command(self.driver))


class BlockedDevice:
    """A Razer device that was found but cannot be used."""

    PERMISSION = "permission"
    UNSUPPORTED = "unsupported"

    def __init__(self, key: str, name: str, reason: str, node: str | None = None):
        self.key = key
        self.name = name
        self.reason = reason
        self.node = node


class _Worker(threading.Thread):
    def __init__(self, transport, on_lost):
        super().__init__(daemon=True)
        self.transport = transport
        self._on_lost = on_lost
        self._pending: dict = {}
        self._cond = threading.Condition()
        self._stopped = False

    def submit(self, key, fn):
        with self._cond:
            self._pending.pop(key, None)
            self._pending[key] = fn
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def run(self):
        while True:
            with self._cond:
                while not self._pending and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    break
                key = next(iter(self._pending))
                fn = self._pending.pop(key)
            try:
                fn()
            except p.ProtocolError as e:
                log.warning("command %s rejected: %s", key, e)
            except OSError as e:
                if e.errno not in _DISCONNECTED:
                    # e.g. EPIPE: the device stalled on this command but is still there
                    log.warning("command %s failed: %s", key, e)
                    continue
                log.info("device lost: %s", e)
                GLib.idle_add(self._on_lost, self)
                break
        self.transport.close()


def _ordered_interfaces(usb: hidraw.UsbDevice, info: DeviceInfo | None):
    """Interfaces known to take lighting reports first, then the rest (for probing)."""
    known, others = [], []
    for interface in usb.interfaces:
        match = info is not None and any(
            (number is None or number == interface.number)
            and (not interface.usages or (page, usage) in interface.usages)
            for number, page, usage in info.interfaces)
        (known if match else others).append(interface)
    # The vendor collection most Razer devices use for control, then by number
    others.sort(key=lambda i: ((0x01, 0x02) not in i.usages, i.number))
    return known, others


def _open_usb_device(usb: hidraw.UsbDevice):
    """Runs on a background thread. Returns (devices info list, worker or None, blocked or None)."""
    pid = usb.product_id
    if pid in catalog.UNSUPPORTED and catalog.lookup(pid) is None:
        return [], None, BlockedDevice(usb.key, catalog.UNSUPPORTED[pid], BlockedDevice.UNSUPPORTED)

    info = catalog.lookup(pid)
    known, others = _ordered_interfaces(usb, info)
    permission_node = None

    def open_transport(interface):
        nonlocal permission_node
        try:
            return hidraw.Transport(interface.node)
        except hidraw.PermissionDenied:
            permission_node = interface.node
        except OSError as e:
            log.info("cannot open %s: %s", interface.node, e)
        return None

    chosen = None
    if known and info is not None:
        transport = open_transport(known[0])
        if transport:
            chosen = (transport, info)
    else:
        for interface in known + others:
            transport = open_transport(interface)
            if not transport:
                continue
            chosen = _probe(transport, usb, info)
            if chosen:
                break
            transport.close()

    name = info.name if info else (usb.product_name or f"Razer device {pid:04x}")
    if chosen is None:
        if permission_node:
            return [], None, BlockedDevice(usb.key, name, BlockedDevice.PERMISSION, permission_node)
        return [], None, BlockedDevice(usb.key, name, BlockedDevice.UNSUPPORTED)

    transport, info = chosen
    found = [_describe(RazerDriver(transport, info), usb.key)]
    for paired_pid in found[0][0].paired_product_ids():
        paired_info = catalog.lookup(paired_pid)
        if paired_pid == pid or paired_info is None:
            continue
        driver = RazerDriver(transport, paired_info, wait_for_response=True)
        found.append(_describe(driver, f"{usb.key}#{paired_pid:04x}", via=info.name))
    return found, transport, None


def _probe(transport, usb, info: DeviceInfo | None):
    """Finds whether this interface answers, and with which transaction ID."""
    candidates = [info.transaction_id] if info else list(_PROBE_TRANSACTION_IDS)
    for tid in candidates:
        probe_info = info or catalog.unknown(usb.product_id, tid)
        driver = RazerDriver(transport, probe_info)
        try:
            if driver.query(p.get_firmware_version()) is None:
                continue
            if info is None:
                regions = driver.query(p.get_effect_regions())
                if regions is None:
                    # Answers, but not the extended lighting protocol we can drive
                    continue
                led = p.LED_BACKLIGHT if p.LED_BACKLIGHT in regions.arguments[::5] else p.LED_ZERO
                probe_info = dataclasses.replace(probe_info, led_override=led,
                                                 name=usb.product_name or probe_info.name)
        except OSError:
            return None
        return transport, probe_info
    return None


def _describe(driver: RazerDriver, key: str, via: str | None = None):
    driver.prepare()
    details = {
        "firmware": driver.firmware(),
        "serial": driver.serial(),
        "effects": driver.effects(),
        "supports_brightness": driver.supports_brightness(),
    }
    return driver, key, via, details


class DeviceManager(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "device-added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self):
        super().__init__()
        self.devices: list[Device] = []
        self.blocked: list[BlockedDevice] = []
        self._workers: dict[str, _Worker] = {}
        self._opening: set[str] = set()
        self._blocked_keys: dict[str, BlockedDevice] = {}
        self.scan()
        GLib.timeout_add_seconds(SCAN_INTERVAL_SECONDS, self._on_scan_timeout)
        GLib.timeout_add_seconds(1, self._on_keepalive_timeout)

    def _on_scan_timeout(self):
        self.scan()
        return GLib.SOURCE_CONTINUE

    def scan(self):
        present = _demo_devices() if DEMO else hidraw.discover(catalog.RAZER_VENDOR_ID)
        present_keys = {usb.key for usb in present}
        changed = False

        for key in list(self._workers):
            if key not in present_keys:
                self._remove_physical(key)
                changed = True
        for key in list(self._blocked_keys):
            if key not in present_keys:
                del self._blocked_keys[key]
                changed = True

        for usb in present:
            blocked = self._blocked_keys.get(usb.key)
            if usb.key in self._workers or usb.key in self._opening:
                continue
            if blocked and blocked.reason == BlockedDevice.UNSUPPORTED:
                continue
            # New device, or one we lacked permission for (the udev rule may now be installed)
            self._opening.add(usb.key)
            threading.Thread(target=self._open_thread, args=(usb,), daemon=True).start()

        if changed:
            self._sync_lists()

    def _open_thread(self, usb):
        if DEMO:
            result = _open_demo(usb)
        else:
            try:
                result = _open_usb_device(usb)
            except OSError as e:
                log.info("opening %s failed: %s", usb.key, e)
                result = ([], None, None)
        GLib.idle_add(self._on_opened, usb, result)

    def _on_opened(self, usb, result):
        self._opening.discard(usb.key)
        found, transport, blocked = result
        if blocked:
            previous = self._blocked_keys.get(usb.key)
            self._blocked_keys[usb.key] = blocked
            if previous is None or previous.reason != blocked.reason:
                self._sync_lists()
            return GLib.SOURCE_REMOVE
        self._blocked_keys.pop(usb.key, None)
        if transport is None:
            return GLib.SOURCE_REMOVE
        worker = _Worker(transport, self._on_worker_lost)
        worker.start()
        self._workers[usb.key] = worker
        for driver, key, via, details in found:
            device = Device(key, driver, worker, via)
            device.firmware = details["firmware"]
            device.serial = details["serial"]
            device.effects = details["effects"]
            device.supports_brightness = details["supports_brightness"]
            self.devices.append(device)
            self.emit("device-added", device)
        self._sync_lists()
        return GLib.SOURCE_REMOVE

    def _remove_physical(self, usb_key: str):
        worker = self._workers.pop(usb_key, None)
        if worker:
            worker.stop()
        self.devices = [d for d in self.devices if d.key != usb_key and not d.key.startswith(usb_key + "#")]

    def _on_worker_lost(self, worker):
        for key, w in list(self._workers.items()):
            if w is worker:
                self._remove_physical(key)
        self._sync_lists()
        return GLib.SOURCE_REMOVE

    def _on_keepalive_timeout(self):
        now = time.monotonic()
        for device in self.devices:
            if device.driver.keepalive_needed() and now - device.last_command > KEEPALIVE_IDLE_SECONDS:
                device.run("keepalive", lambda d: d.keepalive())
        return GLib.SOURCE_CONTINUE

    def _sync_lists(self):
        self.blocked = list(self._blocked_keys.values())
        self.emit("changed")

    def find(self, key: str) -> Device | None:
        return next((d for d in self.devices if d.key == key), None)

    def shutdown(self):
        for key in list(self._workers):
            self._remove_physical(key)


# Demo mode

_DEMO_PRODUCTS = (0x0235, 0x024E, 0x0084)  # BlackWidow Lite, BlackWidow V3, DeathAdder V2


def _demo_devices():
    return [hidraw.UsbDevice(f"demo-{pid:04x}", catalog.RAZER_VENDOR_ID, pid, None) for pid in _DEMO_PRODUCTS]


class _DemoTransport:
    """Answers queries like a cooperative device."""

    def __init__(self):
        self.lock = threading.RLock()
        self._last: p.Report | None = None

    def send_feature(self, report_id, data):
        self._last = p.Report.unpack(data)

    def get_feature(self, report_id, length):
        request = self._last
        if request is None:
            return bytes(length)
        args = b""
        if (request.command_class, request.command_id) == (0x00, 0x81):
            args = bytes([1, 4])
        elif (request.command_class, request.command_id) == (0x00, 0x82):
            args = b"PM2103H0000123"
        elif (request.command_class, request.command_id) == (0x0F, 0x81):
            args = bytes([request.arguments[0], 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09])
        elif (request.command_class, request.command_id) == (0x0F, 0x80):
            args = bytes([0x01, 0, 0, 0, 0, 0x04, 0, 0, 0, 0])
        elif (request.command_class, request.command_id) == (0x00, 0xBF):
            return p.Report(0x00, 0xBF, 0x50, b"", request.transaction_id, p.STATUS_NOT_SUPPORTED).pack()
        return p.Report(request.command_class, request.command_id, len(args) or request.data_size,
                        args, request.transaction_id, p.STATUS_SUCCESS).pack()

    def close(self):
        pass


def _open_demo(usb):
    transport = _DemoTransport()
    driver = RazerDriver(transport, catalog.lookup(usb.product_id))
    return [_describe(driver, usb.key)], transport, None
