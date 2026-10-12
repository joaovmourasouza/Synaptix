"""Lighting commands for Razer devices, ported from OpenRGB's RazerController.

All methods block on USB I/O; call them from a worker thread.
"""

import logging
import time

from . import catalog, db
from . import protocol as p
from .catalog import DeviceInfo
from ..hidraw import Transport

log = logging.getLogger(__name__)

# Devices of the "custom" matrix type, each addressed its own way
TARTARUS_CHROMA = 0x0208
DEATHADDER_CHROMA = 0x0043
NAGA_EPIC_CHROMA = 0x003E
MAMBA_2012 = (0x0024, 0x0025)
ORBWEAVER_CHROMA = 0x0207

# Old devices that take one colour per LED through set_led_rgb/set_led_effect
_SINGLE_LED_DEVICES = {
    TARTARUS_CHROMA: (p.LED_BACKLIGHT,),
    DEATHADDER_CHROMA: (p.LED_SCROLL_WHEEL, p.LED_LOGO),
    NAGA_EPIC_CHROMA: (p.LED_SCROLL_WHEEL, p.LED_BACKLIGHT),
    MAMBA_2012[0]: (p.LED_SCROLL_WHEEL,),
    MAMBA_2012[1]: (p.LED_SCROLL_WHEEL,),
}
# set_led_effect values
_LED_STATIC, _LED_BREATHING, _LED_SPECTRUM = 0, 2, 4

WAVE_LEFT, WAVE_RIGHT = 0x01, 0x02
SPEED_FAST, SPEED_MEDIUM, SPEED_SLOW = 0x01, 0x02, 0x03

_STANDARD_LIKE = (catalog.MATRIX_STANDARD, catalog.MATRIX_LINEAR)
_EXTENDED_LIKE = (catalog.MATRIX_EXTENDED, catalog.MATRIX_EXTENDED_ARGB)


class RazerDriver:
    def __init__(self, transport: Transport, info: DeviceInfo, wait_for_response: bool = False):
        self.transport = transport
        self.info = info
        self.wait_for_response = wait_for_response
        self._firmware_effects: set[int] | None = None

    # Transport

    def _prepare(self, report: p.Report) -> p.Report:
        return report.with_transaction_id(self.info.transaction_id)

    def _read_response(self) -> p.Report | None:
        data = self.transport.get_feature(self.info.report_id, p.REPORT_SIZE)
        try:
            return p.Report.unpack(data)
        except p.ProtocolError:
            return None

    def send(self, report: p.Report):
        report = self._prepare(report)
        self.transport.send_feature(self.info.report_id, report.pack())
        if self.wait_for_response:
            # Receivers relay commands to paired devices and drop commands sent meanwhile
            for _ in range(50):
                time.sleep(0.002)
                response = self._read_response()
                if response and response.answers(report):
                    break
        else:
            # Firmware needs a short gap between reports
            time.sleep(0.001)

    def query(self, report: p.Report) -> p.Report | None:
        report = self._prepare(report)
        with self.transport.lock:
            for _ in range(3):
                time.sleep(0.002)
                self.transport.send_feature(self.info.report_id, report.pack())
                for _ in range(25):
                    time.sleep(0.002)
                    response = self._read_response()
                    if response and response.answers(report):
                        return response if response.status == p.STATUS_SUCCESS else None
        return None

    # Information

    def firmware(self) -> str | None:
        response = self.query(p.get_firmware_version())
        return f"v{response.arguments[0]}.{response.arguments[1]}" if response else None

    def serial(self) -> str | None:
        response = self.query(p.get_serial())
        if not response:
            return None
        text = "".join(chr(b) if 30 <= b <= 126 else " " for b in response.arguments[:22])
        return text.strip() or None

    def paired_product_ids(self) -> list[int]:
        """Devices paired to a wireless receiver; non-receivers reject the command."""
        response = self.query(p.get_wireless_connections())
        if not response:
            return []
        args = response.arguments
        pids = []
        for slot in range(args[0]):
            if slot * 3 + 3 >= len(args):
                break
            pid = (args[slot * 3 + 2] << 8) | args[slot * 3 + 3]
            if pid not in (0x0000, 0xFFFF):
                pids.append(pid)
        return pids

    def prepare(self):
        """Per-device setup after opening, mirroring OpenRGB's constructor."""
        if self.info.product_id in db.SOFTWARE_MODE_ON_OPEN:
            self.send(p.set_device_mode(p.DEVICE_MODE_SOFTWARE))
        if self.info.matrix in _EXTENDED_LIKE:
            self._firmware_effects = self._query_firmware_effects()

    def keepalive_needed(self) -> bool:
        return self.info.product_id in db.KEEPALIVE

    def keepalive(self):
        self.query(p.get_device_mode())

    def _query_firmware_effects(self) -> set[int] | None:
        """Effects the firmware lists for the lighting region(s) we drive."""
        led = self.info.led_id
        if led != p.LED_ZERO:
            regions = [led]
        else:
            response = self.query(p.get_effect_regions())
            if not response:
                return None
            regions = []
            for offset in range(0, len(response.arguments) - 4, 5):
                if response.arguments[offset] == 0x00 or len(regions) >= 16:
                    break
                regions.append(response.arguments[offset])
        supported: set[int] | None = None
        for region in regions:
            response = self.query(p.get_region_effects(region))
            if not response:
                return None
            effects = set(response.arguments[1:min(response.data_size, len(response.arguments))])
            supported = effects if supported is None else supported & effects
        return supported

    # Capabilities

    def _firmware_lists(self, effect: int) -> bool:
        return self._firmware_effects is not None and effect in self._firmware_effects

    def _has_input(self) -> bool:
        return self.info.type in ("keyboard", "mouse", "keypad", "unknown")

    def effects(self) -> list[str]:
        info = self.info
        if info.effects is not None:
            return [e for e in _EFFECT_ORDER if e in info.effects]
        if info.matrix == catalog.MATRIX_NONE:
            return []
        pid = info.product_id
        found = {catalog.OFF, catalog.STATIC, catalog.SPECTRUM}
        if self._firmware_lists(p.EFFECT_BREATHING) or pid not in db.NO_HARDWARE_BREATHING:
            found.add(catalog.BREATHING)
            if pid not in _SINGLE_LED_DEVICES:
                found |= {catalog.BREATHING_DUAL, catalog.BREATHING_RANDOM}
        if self._firmware_lists(p.EFFECT_WAVE) or pid in db.WAVE:
            found.add(catalog.WAVE)
        if self._firmware_lists(p.EFFECT_REACTIVE) and self._has_input():
            found.add(catalog.REACTIVE)
        if self._firmware_lists(p.EFFECT_RIPPLE) and self._has_input():
            found.add(catalog.RIPPLE)
        if self._firmware_lists(p.EFFECT_STARLIGHT):
            found.add(catalog.STARLIGHT)
        if self._firmware_lists(p.EFFECT_FIRE):
            found.add(catalog.FIRE)
        if self.supports_custom_frame():
            found.add(catalog.CUSTOM)
        return [e for e in _EFFECT_ORDER if e in found]

    def supports_brightness(self) -> bool:
        matrix = self.info.matrix
        return matrix in _STANDARD_LIKE + _EXTENDED_LIKE or self.info.product_id == ORBWEAVER_CHROMA

    def supports_custom_frame(self) -> bool:
        info = self.info
        if info.monochrome or info.effects is not None or info.led_count < 1:
            return False
        if info.matrix in (catalog.MATRIX_STANDARD, catalog.MATRIX_EXTENDED, catalog.MATRIX_LINEAR):
            return True
        return info.matrix == catalog.MATRIX_CUSTOM

    # Commands

    def _storage(self, save: bool) -> int:
        return p.SAVE if save else p.NO_SAVE

    def set_brightness(self, value: float, save: bool = False):
        """value is 0..1, scaled to the device's range."""
        level = round(max(0.0, min(1.0, value)) * self.info.max_brightness)
        storage, led, matrix = self._storage(save), self.info.led_id, self.info.matrix
        if matrix in _STANDARD_LIKE or self.info.product_id == ORBWEAVER_CHROMA:
            self.send(p.standard_brightness(storage, led, level))
        elif matrix == catalog.MATRIX_EXTENDED:
            self.send(p.extended_brightness(storage, led, level))
        elif matrix == catalog.MATRIX_EXTENDED_ARGB:
            for channel in p.LED_ARGB_CHANNELS:
                self.send(p.extended_brightness(storage, channel, level))

    def _single_leds(self, colors: list[p.Color] | None, effect: int, storage: int):
        for index, led in enumerate(_SINGLE_LED_DEVICES[self.info.product_id]):
            if colors is not None:
                self.send(p.set_led_rgb(storage, led, colors[min(index, len(colors) - 1)]))
            self.send(p.set_led_effect(storage, led, effect))

    def _dispatch(self, standard, extended, single=None):
        """Sends the variant matching the matrix type; `single` handles old single-LED devices."""
        matrix, pid = self.info.matrix, self.info.product_id
        if matrix in _STANDARD_LIKE or pid == ORBWEAVER_CHROMA:
            if standard:
                self.send(standard())
        elif matrix in _EXTENDED_LIKE:
            if extended:
                self.send(extended())
        elif pid in _SINGLE_LED_DEVICES and single:
            single()

    def set_off(self, save: bool = False):
        s, led = self._storage(save), self.info.led_id
        self._dispatch(p.standard_none, lambda: p.extended_none(s, led),
                       lambda: self._single_leds([(0, 0, 0)], _LED_STATIC, s))

    def set_static(self, color: p.Color, save: bool = False):
        s, led = self._storage(save), self.info.led_id
        self._dispatch(lambda: p.standard_static(color), lambda: p.extended_static(s, led, color),
                       lambda: self._single_leds([color], _LED_STATIC, s))

    def set_breathing(self, color: p.Color, save: bool = False):
        s, led = self._storage(save), self.info.led_id
        self._dispatch(lambda: p.standard_breathing_one(color), lambda: p.extended_breathing_one(s, led, color),
                       lambda: self._single_leds([color], _LED_BREATHING, s))

    def set_breathing_dual(self, color1: p.Color, color2: p.Color, save: bool = False):
        s, led = self._storage(save), self.info.led_id
        self._dispatch(lambda: p.standard_breathing_two(color1, color2),
                       lambda: p.extended_breathing_two(s, led, color1, color2))

    def set_breathing_random(self, save: bool = False):
        s, led = self._storage(save), self.info.led_id
        self._dispatch(p.standard_breathing_random, lambda: p.extended_breathing_random(s, led))

    def set_spectrum(self, save: bool = False):
        s, led = self._storage(save), self.info.led_id
        self._dispatch(p.standard_spectrum, lambda: p.extended_spectrum(s, led),
                       lambda: self._single_leds(None, _LED_SPECTRUM, s))

    def set_wave(self, direction: int, save: bool = False):
        s, led = self._storage(save), self.info.led_id
        self._dispatch(lambda: p.standard_wave(direction), lambda: p.extended_wave(s, led, direction))

    def set_reactive(self, speed: int, color: p.Color):
        self.send(p.extended_effect(p.NO_SAVE, self.info.led_id, p.EFFECT_REACTIVE, 0x00, speed, (color,)))

    def set_starlight(self, speed: int, colors: tuple[p.Color, ...]):
        """No colours: random; one or two colours otherwise."""
        self.send(p.extended_effect(p.NO_SAVE, self.info.led_id, p.EFFECT_STARLIGHT, len(colors), speed, colors))

    def set_ripple(self, color: p.Color):
        self.send(p.extended_effect(p.NO_SAVE, self.info.led_id, p.EFFECT_RIPPLE, 0x00, 0x1E, (color,)))

    def set_fire(self):
        # Firmware rejects fire without a colour; Synapse always sends red
        self.send(p.extended_effect(p.NO_SAVE, self.info.led_id, p.EFFECT_FIRE, 0x00, 0x00, ((0xFF, 0, 0),)))

    def set_custom_frame(self, colors: list[p.Color]):
        """colors: one entry per LED, zones concatenated in catalog order."""
        info = self.info
        rows, cols = info.rows, info.cols
        flat = list(colors) + [(0, 0, 0)] * max(0, rows * cols - len(colors))
        pid = info.product_id

        if pid in _SINGLE_LED_DEVICES:
            self._single_leds(flat, _LED_STATIC, p.NO_SAVE)
            return

        for row in range(rows):
            rgb = bytes(c for color in flat[row * cols:(row + 1) * cols] for c in color)
            time.sleep(0.001)
            if info.matrix == catalog.MATRIX_STANDARD:
                self.send(p.standard_custom_frame(row, 0, cols - 1, rgb))
            elif info.matrix == catalog.MATRIX_EXTENDED:
                self.send(p.extended_custom_frame(row, 0, cols - 1, rgb))
            elif info.matrix == catalog.MATRIX_LINEAR:
                self.send(p.linear_custom_frame(0, cols - 1, rgb))
            elif pid == ORBWEAVER_CHROMA:
                # Hardware layout has a border and a gap in the last row; present it as 4x5
                if row != 3:
                    self.send(p.standard_custom_frame(row + 1, 1, cols, rgb))
                else:
                    adjusted = rgb[:3] + bytes(3) + rgb[3:15]
                    self.send(p.standard_custom_frame(row + 1, 1, cols + 1, adjusted))
        time.sleep(0.001)
        if info.matrix in _STANDARD_LIKE or pid == ORBWEAVER_CHROMA:
            self.send(p.standard_custom_mode(p.NO_SAVE))
        elif info.matrix == catalog.MATRIX_EXTENDED:
            self.send(p.extended_custom_mode())


_EFFECT_ORDER = (
    catalog.STATIC, catalog.BREATHING, catalog.BREATHING_DUAL, catalog.BREATHING_RANDOM,
    catalog.SPECTRUM, catalog.WAVE, catalog.REACTIVE, catalog.RIPPLE, catalog.STARLIGHT,
    catalog.FIRE, catalog.CUSTOM, catalog.OFF,
)
