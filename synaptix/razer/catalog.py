"""Static knowledge about Razer devices, from the generated OpenRGB table plus local additions."""

from dataclasses import dataclass, field

from . import db

RAZER_VENDOR_ID = 0x1532

# Matrix types
MATRIX_NONE = "none"
MATRIX_STANDARD = "standard"
MATRIX_EXTENDED = "extended"
MATRIX_LINEAR = "linear"
MATRIX_EXTENDED_ARGB = "extended_argb"
MATRIX_CUSTOM = "custom"

# Effects a device may offer
OFF = "off"
STATIC = "static"
BREATHING = "breathing"
BREATHING_DUAL = "breathing_dual"
BREATHING_RANDOM = "breathing_random"
SPECTRUM = "spectrum"
WAVE = "wave"
REACTIVE = "reactive"
STARLIGHT = "starlight"
RIPPLE = "ripple"
FIRE = "fire"
CUSTOM = "custom"


@dataclass(frozen=True)
class Zone:
    name: str
    kind: str  # "single", "linear" or "matrix"
    rows: int
    cols: int


@dataclass(frozen=True)
class DeviceInfo:
    product_id: int
    name: str
    type: str
    matrix: str
    transaction_id: int
    rows: int
    cols: int
    zones: tuple[Zone, ...]
    # (interface number or None, usage page, usage) of the HID collection taking lighting reports
    interfaces: tuple[tuple[int | None, int, int], ...] = ()
    # Single-colour devices (white backlight): no colour choice
    monochrome: bool = False
    # Fixed effect list, for devices whose firmware cannot be queried
    effects: frozenset[str] | None = None
    # Use SAVE storage when applying settings (OpenRazer does this for these devices)
    save_by_default: bool = False
    led_override: int | None = None
    verified: bool = field(default=True, compare=False)

    @property
    def report_id(self) -> int:
        return 0x07 if self.product_id in db.REPORT_ID_7 else 0x00

    @property
    def led_id(self) -> int:
        if self.led_override is not None:
            return self.led_override
        return 0x00 if self.product_id in db.LED_ID_ZERO else 0x05

    @property
    def max_brightness(self) -> int:
        return 100 if self.product_id in db.MAX_BRIGHTNESS_100 else 255

    @property
    def led_count(self) -> int:
        return sum(z.rows * z.cols for z in self.zones)


# Devices missing from OpenRGB (it only lists RGB devices). Values from OpenRazer.
_EXTRA = {
    0x0235: DeviceInfo(
        product_id=0x0235, name="Razer BlackWidow Lite", type="keyboard", matrix=MATRIX_EXTENDED,
        transaction_id=0x3F, rows=1, cols=1, zones=(Zone("Keyboard", "single", 1, 1),),
        monochrome=True, effects=frozenset({OFF, STATIC, BREATHING}), save_by_default=True,
    ),
}

# Razer devices using other protocols (Kraken headsets, Hanbo cooler, ARGB controller, Bluetooth)
UNSUPPORTED = {pid: entry[0] for pid, entry in db.DEVICES.items() if pid not in db.INTERFACES}


def lookup(product_id: int) -> DeviceInfo | None:
    if product_id in _EXTRA:
        return _EXTRA[product_id]
    if product_id not in db.INTERFACES:
        return None
    name, kind, matrix, tid, rows, cols, zones = db.DEVICES[product_id]
    return DeviceInfo(product_id, name, kind, matrix, tid, rows, cols,
                      tuple(Zone(*z) for z in zones), db.INTERFACES[product_id])


def unknown(product_id: int, transaction_id: int) -> DeviceInfo:
    """A device we know nothing about but that answered the extended protocol."""
    return DeviceInfo(product_id, f"Razer device {product_id:04x}", "unknown", MATRIX_EXTENDED,
                      transaction_id, 1, 1, (Zone("Device", "single", 1, 1),), verified=False)


def supported_count() -> int:
    return len(db.INTERFACES) + len(_EXTRA)
