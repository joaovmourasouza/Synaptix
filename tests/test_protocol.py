import os
import tempfile
import threading
import unittest

from synaptix import hidraw
from synaptix.razer import catalog, db
from synaptix.razer import protocol as p
from synaptix.razer.driver import RazerDriver, WAVE_RIGHT


class FakeTransport:
    """Records sent reports and answers every query with success."""

    def __init__(self, effects=(0x01, 0x02, 0x03, 0x04)):
        self.lock = threading.RLock()
        self.sent: list[tuple[int, p.Report]] = []
        self._effects = bytes(effects)

    def send_feature(self, report_id, data):
        self.sent.append((report_id, p.Report.unpack(data)))

    def get_feature(self, report_id, length):
        request = self.sent[-1][1]
        args = b""
        if (request.command_class, request.command_id) == (0x0F, 0x81):
            args = bytes([request.arguments[0]]) + self._effects
        return p.Report(request.command_class, request.command_id, len(args) or request.data_size,
                        args, request.transaction_id, p.STATUS_SUCCESS).pack()

    def close(self):
        pass

    def commands(self):
        return [(r.command_class, r.command_id, r.arguments[:r.data_size]) for _, r in self.sent]


class ReportTests(unittest.TestCase):
    def test_layout_and_crc(self):
        data = p.extended_brightness(p.SAVE, p.LED_BACKLIGHT, 200).with_transaction_id(0x3F).pack()
        self.assertEqual(len(data), p.REPORT_SIZE)
        self.assertEqual(data[1], 0x3F)
        self.assertEqual(data[5:8], bytes([0x03, 0x0F, 0x04]))
        self.assertEqual(data[8:11], bytes([0x01, 0x05, 200]))
        # XOR of bytes 2..87, transaction id excluded
        self.assertEqual(data[88], 0x03 ^ 0x0F ^ 0x04 ^ 0x01 ^ 0x05 ^ 0xC8)

    def test_bad_checksum_rejected(self):
        data = bytearray(p.get_serial().pack())
        data[88] ^= 0xFF
        with self.assertRaises(p.ProtocolError):
            p.Report.unpack(bytes(data))

    def test_extended_builders_match_openrgb(self):
        self.assertEqual(p.extended_static(0, 5, (1, 2, 3)).arguments, bytes([0, 5, 1, 0, 0, 1, 1, 2, 3]))
        self.assertEqual(p.extended_breathing_one(1, 5, (9, 9, 9)).arguments, bytes([1, 5, 2, 1, 0, 1, 9, 9, 9]))
        report = p.extended_breathing_two(0, 0, (1, 1, 1), (2, 2, 2))
        self.assertEqual((report.data_size, report.arguments),
                         (0x0C, bytes([0, 0, 2, 2, 0, 2, 1, 1, 1, 2, 2, 2])))
        self.assertEqual(p.extended_spectrum(0, 5).arguments, bytes([0, 5, 3]))
        self.assertEqual(p.extended_wave(0, 5, 2).arguments, bytes([0, 5, 4, 2, 0x28]))
        reactive = p.extended_effect(0, 5, p.EFFECT_REACTIVE, 0, 2, ((7, 8, 9),))
        self.assertEqual((reactive.data_size, reactive.arguments), (9, bytes([0, 5, 5, 0, 2, 1, 7, 8, 9])))

    def test_custom_frames(self):
        rgb = bytes(range(9))
        ext = p.extended_custom_frame(4, 0, 2, rgb)
        self.assertEqual((ext.command_class, ext.command_id, ext.data_size), (0x0F, 0x03, 14))
        self.assertEqual(ext.arguments, bytes([0, 0, 4, 0, 2]) + rgb)
        std = p.standard_custom_frame(1, 0, 2, rgb)
        self.assertEqual((std.command_id, std.data_size, std.arguments), (0x0B, 13, bytes([0xFF, 1, 0, 2]) + rgb))
        lin = p.linear_custom_frame(0, 2, rgb)
        self.assertEqual((lin.command_id, lin.data_size, lin.arguments), (0x0C, 0x32, bytes([0, 2]) + rgb))

    def test_ioctl_numbers(self):
        # HIDIOCSFEATURE(91) / HIDIOCGFEATURE(91) from <linux/hidraw.h>
        self.assertEqual(hidraw.hidiocsfeature(91), 0xC05B4806)
        self.assertEqual(hidraw.hidiocgfeature(91), 0xC05B4807)


class DescriptorTests(unittest.TestCase):
    def test_top_level_collections(self):
        descriptor = bytes([
            0x05, 0x01, 0x09, 0x06, 0xA1, 0x01,  # Generic Desktop / Keyboard, Application
            0x05, 0x07, 0xA1, 0x00, 0xC0,        # nested physical collection, ignored
            0xC0,
            0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0xC0,  # vendor page 0xFF00 / 0x01
            0x0B, 0x02, 0x00, 0x01, 0x00, 0xA1, 0x01, 0xC0,  # extended usage: page 1, usage 2
        ])
        self.assertEqual(hidraw.parse_top_level_usages(descriptor),
                         [(0x01, 0x06), (0xFF00, 0x01), (0x01, 0x02)])


class DiscoveryTests(unittest.TestCase):
    def test_groups_interfaces_and_ignores_other_vendors(self):
        with tempfile.TemporaryDirectory() as root:
            usb = os.path.join(root, "devices", "usb1", "1-2")
            class_dir = os.path.join(root, "class")
            os.makedirs(class_dir)
            for interface, node in ((0, "hidraw3"), (1, "hidraw4"), (2, "hidraw5")):
                intf = os.path.join(usb, f"1-2:1.{interface}")
                hid = os.path.join(intf, f"0003:1532:0235.000{interface}")
                os.makedirs(hid)
                with open(os.path.join(intf, "bInterfaceNumber"), "w") as f:
                    f.write(f"0{interface}\n")
                with open(os.path.join(hid, "uevent"), "w") as f:
                    f.write("HID_ID=0003:00001532:00000235\n")
                with open(os.path.join(hid, "report_descriptor"), "wb") as f:
                    f.write(bytes([0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0xC0]))
                os.makedirs(os.path.join(class_dir, node))
                os.symlink(hid, os.path.join(class_dir, node, "device"))
            with open(os.path.join(usb, "product"), "w") as f:
                f.write("Razer BlackWidow Lite\n")
            touchpad = os.path.join(root, "devices", "i2c", "0018:06CB:CE65.0001")
            os.makedirs(touchpad)
            with open(os.path.join(touchpad, "uevent"), "w") as f:
                f.write("HID_ID=0018:000006CB:0000CE65\n")
            os.makedirs(os.path.join(class_dir, "hidraw0"))
            os.symlink(touchpad, os.path.join(class_dir, "hidraw0", "device"))

            original = hidraw.SYS_HIDRAW
            hidraw.SYS_HIDRAW = class_dir
            try:
                found = hidraw.discover(catalog.RAZER_VENDOR_ID)
            finally:
                hidraw.SYS_HIDRAW = original

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].product_id, 0x0235)
        self.assertEqual(found[0].product_name, "Razer BlackWidow Lite")
        self.assertEqual([(i.number, i.node, i.usages) for i in found[0].interfaces],
                         [(0, "/dev/hidraw3", [(1, 2)]), (1, "/dev/hidraw4", [(1, 2)]),
                          (2, "/dev/hidraw5", [(1, 2)])])


class CatalogTests(unittest.TestCase):
    def test_database_is_consistent(self):
        self.assertGreater(len(db.DEVICES), 150)
        for pid, (name, kind, matrix, tid, rows, cols, zones) in db.DEVICES.items():
            self.assertIn(matrix, ("none", "standard", "extended", "linear", "extended_argb", "custom"), name)
            self.assertTrue(name, hex(pid))
        self.assertTrue(set(db.INTERFACES) <= set(db.DEVICES))

    def test_lite_is_monochrome_extended(self):
        lite = catalog.lookup(0x0235)
        self.assertTrue(lite.monochrome)
        self.assertEqual((lite.matrix, lite.transaction_id, lite.led_id), ("extended", 0x3F, 0x05))

    def test_headsets_with_other_protocols_are_unsupported(self):
        kraken = next(pid for pid, entry in db.DEVICES.items() if entry[0] == "Razer Kraken V3")
        self.assertIsNone(catalog.lookup(kraken))
        self.assertIn(kraken, catalog.UNSUPPORTED)


class DriverTests(unittest.TestCase):
    def test_lite_effects_and_commands(self):
        transport = FakeTransport()
        driver = RazerDriver(transport, catalog.lookup(0x0235))
        driver.prepare()
        self.assertEqual(driver.effects(), ["static", "breathing", "off"])
        self.assertFalse(driver.supports_custom_frame())
        transport.sent.clear()
        driver.set_static((255, 255, 255), save=True)
        driver.set_brightness(0.5, save=True)
        self.assertEqual(transport.commands(), [
            (0x0F, 0x02, bytes([1, 5, 1, 0, 0, 1, 255, 255, 255])),
            (0x0F, 0x04, bytes([1, 5, 128])),
        ])
        self.assertTrue(all(r.transaction_id == 0x3F for _, r in transport.sent))

    def test_extended_keyboard_uses_firmware_effect_list(self):
        transport = FakeTransport(effects=(0x01, 0x02, 0x03, 0x04, 0x05, 0x07))
        driver = RazerDriver(transport, catalog.lookup(0x024E))  # BlackWidow V3
        driver.prepare()
        effects = driver.effects()
        for effect in ("static", "breathing", "spectrum", "wave", "reactive", "starlight", "custom", "off"):
            self.assertIn(effect, effects)
        self.assertNotIn("fire", effects)

    def test_extended_custom_frame_sends_each_row_then_mode(self):
        transport = FakeTransport()
        info = catalog.lookup(0x024E)
        driver = RazerDriver(transport, info)
        driver.set_custom_frame([(1, 2, 3)] * (info.rows * info.cols))
        commands = transport.commands()
        self.assertEqual(len(commands), info.rows + 1)
        self.assertEqual(commands[0][:2], (0x0F, 0x03))
        self.assertEqual(commands[-1], (0x0F, 0x02, bytes([0, 0, 8]) + bytes(9)))

    def test_standard_matrix_wave(self):
        transport = FakeTransport()
        driver = RazerDriver(transport, catalog.lookup(0x0203))  # BlackWidow Chroma
        driver.set_wave(WAVE_RIGHT)
        self.assertEqual(transport.commands(), [(0x03, 0x0A, bytes([0x01, WAVE_RIGHT]))])

    def test_single_led_mouse_static(self):
        transport = FakeTransport()
        driver = RazerDriver(transport, catalog.lookup(0x0043))  # DeathAdder Chroma
        driver.set_static((10, 20, 30))
        self.assertEqual(transport.commands(), [
            (0x03, 0x01, bytes([0, 0x01, 10, 20, 30])), (0x03, 0x02, bytes([0, 0x01, 0])),
            (0x03, 0x01, bytes([0, 0x04, 10, 20, 30])), (0x03, 0x02, bytes([0, 0x04, 0])),
        ])

    def test_led_zero_mouse_reads_all_regions(self):
        transport = FakeTransport()
        info = catalog.lookup(0x0084)  # DeathAdder V2
        self.assertEqual(info.led_id, 0x00)
        driver = RazerDriver(transport, info)
        driver.prepare()
        self.assertEqual(transport.commands()[0][:2], (0x0F, 0x80))



class ImportTests(unittest.TestCase):
    def test_all_modules_import(self):
        import importlib
        import pkgutil

        import synaptix
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
        except (ImportError, ValueError):
            self.skipTest("GTK 4 / libadwaita not available")
        for module in pkgutil.walk_packages(synaptix.__path__, "synaptix."):
            if module.name != "synaptix.__main__":
                importlib.import_module(module.name)


if __name__ == "__main__":
    unittest.main()
