"""Razer USB HID control protocol.

Every command is a 90-byte feature report. Layout and command values follow
OpenRazer (driver/razercommon.h, razerchromacommon.c) and OpenRGB
(Controllers/RazerController/RazerController.cpp).
"""

from dataclasses import dataclass

REPORT_SIZE = 90
MAX_ARGS = 80

# Response status
STATUS_NEW = 0x00
STATUS_BUSY = 0x01
STATUS_SUCCESS = 0x02
STATUS_FAILURE = 0x03
STATUS_TIMEOUT = 0x04
STATUS_NOT_SUPPORTED = 0x05

# Storage: SAVE persists across power cycles
NO_SAVE = 0x00
SAVE = 0x01

# LED identifiers
LED_ZERO = 0x00
LED_SCROLL_WHEEL = 0x01
LED_LOGO = 0x04
LED_BACKLIGHT = 0x05
LED_ARGB_CHANNELS = (0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F)

# Extended matrix effect identifiers (command 0x0F:0x02)
EFFECT_OFF = 0x00
EFFECT_STATIC = 0x01
EFFECT_BREATHING = 0x02
EFFECT_SPECTRUM = 0x03
EFFECT_WAVE = 0x04
EFFECT_REACTIVE = 0x05
EFFECT_RIPPLE = 0x06
EFFECT_STARLIGHT = 0x07
EFFECT_CUSTOM_FRAME = 0x08
EFFECT_FIRE = 0x09

DEVICE_MODE_HARDWARE = 0x00
DEVICE_MODE_SOFTWARE = 0x03

Color = tuple[int, int, int]


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class Report:
    command_class: int
    command_id: int
    data_size: int
    arguments: bytes = b""
    transaction_id: int = 0xFF
    status: int = STATUS_NEW

    def with_transaction_id(self, transaction_id: int) -> "Report":
        return Report(self.command_class, self.command_id, self.data_size, self.arguments,
                      transaction_id, self.status)

    def pack(self) -> bytes:
        if len(self.arguments) > MAX_ARGS:
            raise ProtocolError("too many arguments")
        buf = bytearray(REPORT_SIZE)
        buf[0] = self.status
        buf[1] = self.transaction_id
        # bytes 2-3: remaining packets, byte 4: protocol type (both zero)
        buf[5] = self.data_size
        buf[6] = self.command_class
        buf[7] = self.command_id
        buf[8:8 + len(self.arguments)] = self.arguments
        buf[88] = crc(buf)
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> "Report":
        if len(data) != REPORT_SIZE:
            raise ProtocolError(f"expected {REPORT_SIZE} bytes, got {len(data)}")
        if data[88] != crc(data):
            raise ProtocolError("bad checksum")
        return cls(data[6], data[7], data[5], bytes(data[8:88]), data[1], data[0])

    def answers(self, request: "Report") -> bool:
        """True if this is the final response to `request`."""
        return (self.transaction_id == request.transaction_id
                and self.command_class == request.command_class
                and self.command_id == request.command_id
                and self.status not in (STATUS_NEW, STATUS_BUSY))


def crc(report: bytes) -> int:
    """XOR of bytes 2..87."""
    value = 0
    for byte in report[2:88]:
        value ^= byte
    return value


def _args(size: int, values: dict[int, int] | None = None, data: bytes = b"", data_offset: int = 0) -> bytes:
    buf = bytearray(size)
    for index, value in (values or {}).items():
        buf[index] = value
    buf[data_offset:data_offset + len(data)] = data
    return bytes(buf)


# General

def get_firmware_version() -> Report:
    return Report(0x00, 0x81, 0x02)


def get_serial() -> Report:
    return Report(0x00, 0x82, 0x16)


def get_device_mode() -> Report:
    return Report(0x00, 0x84, 0x02)


def set_device_mode(mode: int) -> Report:
    return Report(0x00, 0x04, 0x02, bytes([mode, 0x00]))


def get_wireless_connections() -> Report:
    return Report(0x00, 0xBF, 0x50)


def get_effect_regions() -> Report:
    return Report(0x0F, 0x80, 0x50)


def get_region_effects(region: int) -> Report:
    return Report(0x0F, 0x81, 0x50, bytes([region]))


# Extended matrix (command class 0x0F)

def extended_brightness(storage: int, led: int, value: int) -> Report:
    return Report(0x0F, 0x04, 0x03, bytes([storage, led, value]))


def extended_effect(storage: int, led: int, effect: int, flags: int = 0, rate: int = 0,
                    colors: tuple[Color, ...] = ()) -> Report:
    """Generic effect report: storage, LED, effect, flags, rate, colour count, colours."""
    data = bytes(c for color in colors for c in color)
    return Report(0x0F, 0x02, 0x06 + 3 * len(colors),
                  bytes([storage, led, effect, flags, rate, len(colors)]) + data)


def extended_none(storage: int, led: int) -> Report:
    return Report(0x0F, 0x02, 0x06, bytes([storage, led, EFFECT_OFF]))


def extended_static(storage: int, led: int, color: Color) -> Report:
    return Report(0x0F, 0x02, 0x09, _args(9, {0: storage, 1: led, 2: EFFECT_STATIC, 5: 0x01}, bytes(color), 6))


def extended_breathing_one(storage: int, led: int, color: Color) -> Report:
    return Report(0x0F, 0x02, 0x09,
                  _args(9, {0: storage, 1: led, 2: EFFECT_BREATHING, 3: 0x01, 5: 0x01}, bytes(color), 6))


def extended_breathing_two(storage: int, led: int, color1: Color, color2: Color) -> Report:
    return Report(0x0F, 0x02, 0x0C,
                  _args(12, {0: storage, 1: led, 2: EFFECT_BREATHING, 3: 0x02, 5: 0x02},
                        bytes(color1) + bytes(color2), 6))


def extended_breathing_random(storage: int, led: int) -> Report:
    return Report(0x0F, 0x02, 0x06, bytes([storage, led, EFFECT_BREATHING]))


def extended_spectrum(storage: int, led: int) -> Report:
    return Report(0x0F, 0x02, 0x06, bytes([storage, led, EFFECT_SPECTRUM]))


def extended_wave(storage: int, led: int, direction: int) -> Report:
    return Report(0x0F, 0x02, 0x06, bytes([storage, led, EFFECT_WAVE, direction, 0x28]))


def extended_custom_mode() -> Report:
    return Report(0x0F, 0x02, 0x0C, bytes([0x00, 0x00, EFFECT_CUSTOM_FRAME]))


def extended_custom_frame(row: int, start: int, stop: int, rgb: bytes) -> Report:
    length = (stop + 1 - start) * 3
    return Report(0x0F, 0x03, length + 5, _args(5, {2: row, 3: start, 4: stop}) + rgb[:length])


# Standard matrix (command class 0x03)

def standard_brightness(storage: int, led: int, value: int) -> Report:
    return Report(0x03, 0x03, 0x03, bytes([storage, led, value]))


def standard_none() -> Report:
    return Report(0x03, 0x0A, 0x01, bytes([0x00]))


def standard_static(color: Color) -> Report:
    return Report(0x03, 0x0A, 0x04, bytes([0x06, *color]))


def standard_breathing_one(color: Color) -> Report:
    return Report(0x03, 0x0A, 0x08, bytes([0x03, 0x01, *color]))


def standard_breathing_two(color1: Color, color2: Color) -> Report:
    return Report(0x03, 0x0A, 0x08, bytes([0x03, 0x02, *color1, *color2]))


def standard_breathing_random() -> Report:
    return Report(0x03, 0x0A, 0x08, bytes([0x03, 0x03]))


def standard_spectrum() -> Report:
    return Report(0x03, 0x0A, 0x01, bytes([0x04]))


def standard_wave(direction: int) -> Report:
    return Report(0x03, 0x0A, 0x02, bytes([0x01, direction]))


def standard_custom_mode(storage: int) -> Report:
    return Report(0x03, 0x0A, 0x02, bytes([0x05, storage]))


def standard_custom_frame(row: int, start: int, stop: int, rgb: bytes) -> Report:
    length = (stop + 1 - start) * 3
    return Report(0x03, 0x0B, length + 4, bytes([0xFF, row, start, stop]) + rgb[:length])


def linear_custom_frame(start: int, stop: int, rgb: bytes) -> Report:
    length = (stop + 1 - start) * 3
    return Report(0x03, 0x0C, 0x32, bytes([start, stop]) + rgb[:length])


# Single LEDs (older devices)

def set_led_rgb(storage: int, led: int, color: Color) -> Report:
    return Report(0x03, 0x01, 0x05, bytes([storage, led, *color]))


def set_led_effect(storage: int, led: int, effect: int) -> Report:
    return Report(0x03, 0x02, 0x03, bytes([storage, led, min(effect, 5)]))
