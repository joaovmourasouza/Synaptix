"""Turns per-device settings into commands, including app-driven effects and dimming."""

import colorsys
import math
import random
import time

from gi.repository import GLib, GObject

from . import config
from .controller import Device, DeviceManager
from .razer import catalog
from .razer.driver import SPEED_MEDIUM, WAVE_LEFT, WAVE_RIGHT

FRAME_MS = 33
# Period of the firmware breathing effect, used to animate the preview
HARDWARE_BREATH_PERIOD = 4.0
WHITE = (0xFF, 0xFF, 0xFF)


def pulse_factor(elapsed: float, period: float) -> float:
    return 0.5 + 0.5 * math.cos(2 * math.pi * elapsed / period)


class DeviceLighting:
    def __init__(self, device: Device, settings: config.DeviceSettings):
        self.device = device
        self.settings = settings
        self._dim = 1.0
        self._pulse = 1.0
        self._pulse_start = time.monotonic()
        self._pulse_source = 0
        self._last_level: int | None = None
        self._dark = False
        self._seed = random.random() * 1000
        if self.settings.effect not in self.effects():
            self.settings.effect = self.effects()[0] if self.effects() else catalog.OFF
        self._update_pulse_timer()

    def effects(self) -> list[str]:
        effects = list(self.device.effects)
        if self.device.supports_brightness and catalog.STATIC in effects:
            index = max((effects.index(e) + 1 for e in (catalog.BREATHING, catalog.BREATHING_DUAL,
                                                       catalog.BREATHING_RANDOM) if e in effects), default=1)
            effects.insert(index, config.PULSE)
        return effects

    def color(self, which: int = 1):
        if self.device.info.monochrome:
            return WHITE
        return config.parse_color(self.settings.color if which == 1 else self.settings.color2)

    def custom_colors(self) -> list[tuple[int, int, int]]:
        count = self.device.info.led_count
        colors = [config.parse_color(c) for c in self.settings.custom[:count]]
        return colors + [self.color()] * (count - len(colors))

    # Applying

    def apply(self, save: bool = False):
        save = save or self.device.info.save_by_default
        self._last_level = None
        self._dark = False
        self._send_effect(save)
        self._send_brightness(save)

    def _send_effect(self, save: bool = False):
        s = self.settings
        effect = s.effect
        color, color2 = self.color(1), self.color(2)
        speed = s.speed or SPEED_MEDIUM
        direction = WAVE_LEFT if s.direction == "left" else WAVE_RIGHT
        starlight = (color, color2)[:s.starlight_colors]
        custom = self.custom_colors()
        commands = {
            catalog.STATIC: lambda d: d.set_static(color, save),
            config.PULSE: lambda d: d.set_static(color, save),
            catalog.BREATHING: lambda d: d.set_breathing(color, save),
            catalog.BREATHING_DUAL: lambda d: d.set_breathing_dual(color, color2, save),
            catalog.BREATHING_RANDOM: lambda d: d.set_breathing_random(save),
            catalog.SPECTRUM: lambda d: d.set_spectrum(save),
            catalog.WAVE: lambda d: d.set_wave(direction, save),
            catalog.REACTIVE: lambda d: d.set_reactive(speed, color),
            catalog.STARLIGHT: lambda d: d.set_starlight(speed, starlight),
            catalog.RIPPLE: lambda d: d.set_ripple(color),
            catalog.FIRE: lambda d: d.set_fire(),
            catalog.CUSTOM: lambda d: d.set_custom_frame(custom),
            catalog.OFF: lambda d: d.set_off(save),
        }
        self.device.run("effect", commands.get(effect, commands[catalog.OFF]))

    def _level(self) -> float:
        level = self.settings.brightness / 100 * self._dim
        if self.settings.effect == config.PULSE:
            level *= self._pulse
        return level

    def _send_brightness(self, save: bool = False):
        if self.settings.effect == catalog.OFF:
            return
        if not self.device.supports_brightness:
            # Without brightness control, idle dimming switches the lighting off and on
            dark = self._dim < 0.05
            if dark != self._dark:
                self._dark = dark
                if dark:
                    self.device.run("effect", lambda d: d.set_off())
                else:
                    self._send_effect()
            return
        level = self._level()
        quantized = round(level * 255)
        if quantized == self._last_level and not save:
            return
        self._last_level = quantized
        self.device.run("brightness", lambda d: d.set_brightness(level, save))

    # Changes from the UI

    def set_effect(self, effect: str):
        self.settings.effect = effect
        self._update_pulse_timer()
        self.apply()

    def refresh_effect(self):
        """Re-sends the current effect after a colour, speed or LED change."""
        self._send_effect()

    def set_brightness(self, percent: int):
        self.settings.brightness = percent
        self._send_brightness()

    def set_dim(self, factor: float):
        if not self.settings.dim_when_idle:
            factor = 1.0
        factor = max(0.0, min(1.0, factor))
        if factor != self._dim:
            self._dim = factor
            self._send_brightness()

    def stop(self):
        if self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0

    def _update_pulse_timer(self):
        wanted = self.settings.effect == config.PULSE
        if wanted and not self._pulse_source:
            self._pulse_start = time.monotonic()
            self._pulse_source = GLib.timeout_add(FRAME_MS, self._on_pulse_frame)
        elif not wanted and self._pulse_source:
            self.stop()
            self._pulse = 1.0

    def _on_pulse_frame(self):
        self._pulse = pulse_factor(time.monotonic() - self._pulse_start, self.settings.pulse_period)
        self._send_brightness()
        return GLib.SOURCE_CONTINUE

    # Preview

    def preview_colors(self) -> list[tuple[float, float, float]]:
        """Approximate current colour of every LED, 0..1 per channel, for the on-screen preview."""
        info = self.device.info
        count = max(1, info.led_count)
        effect = self.settings.effect
        t = time.monotonic()
        level = self.settings.brightness / 100 * self._dim if self.device.supports_brightness else 1.0
        if self._dark:
            level = 0.0
        c1 = tuple(v / 255 for v in self.color(1))
        c2 = tuple(v / 255 for v in self.color(2))
        breath = pulse_factor(t, HARDWARE_BREATH_PERIOD)

        if effect == catalog.OFF:
            return [(0.0, 0.0, 0.0)] * count
        if effect == catalog.CUSTOM:
            colors = [tuple(v / 255 for v in c) for c in self.custom_colors()] or [c1]
        elif effect == config.PULSE:
            colors = [c1] * count
            level *= self._pulse
        elif effect == catalog.BREATHING:
            colors = [c1] * count
            level *= breath
        elif effect == catalog.BREATHING_DUAL:
            colors = [c1 if int(t / HARDWARE_BREATH_PERIOD) % 2 == 0 else c2] * count
            level *= breath
        elif effect == catalog.BREATHING_RANDOM:
            hue = (math.sin(int(t / HARDWARE_BREATH_PERIOD) * 12.9898 + self._seed) * 43758.5453) % 1
            colors = [colorsys.hsv_to_rgb(hue, 1, 1)] * count
            level *= breath
        elif effect == catalog.SPECTRUM:
            colors = [colorsys.hsv_to_rgb((t / 10) % 1, 1, 1)] * count
        elif effect == catalog.WAVE:
            sign = -1 if self.settings.direction == "left" else 1
            columns = self._led_columns()
            colors = [colorsys.hsv_to_rgb((sign * t / 4 - x) % 1, 1, 1) for x in columns]
        elif effect == catalog.STARLIGHT:
            palette = [c1, c2][:self.settings.starlight_colors] or None
            colors = []
            for i in range(count):
                twinkle = max(0.0, math.sin(t * (4 - self.settings.speed) + i * 7.31 + self._seed)) ** 12
                base = palette[i % len(palette)] if palette else colorsys.hsv_to_rgb((i * 0.137) % 1, 1, 1)
                colors.append(tuple(v * twinkle for v in base))
        elif effect == catalog.FIRE:
            colors = []
            for i in range(count):
                flicker = 0.6 + 0.4 * math.sin(t * 9 + i * 1.7 + self._seed) * math.sin(t * 4.3 + i)
                colors.append((1.0 * flicker, 0.35 * flicker * flicker, 0.0))
        elif effect in (catalog.REACTIVE, catalog.RIPPLE):
            colors = [tuple(v * 0.25 for v in c1)] * count
        else:
            colors = [c1] * count
        return [tuple(v * level for v in c) for c in colors]

    def _led_columns(self) -> list[float]:
        """Horizontal position of every LED, 0..1, used by the wave preview."""
        positions = []
        for zone in self.device.info.zones:
            for row in range(zone.rows):
                for col in range(zone.cols):
                    positions.append(col / max(1, zone.cols))
        return positions or [0.0]


class LightingManager(GObject.Object):
    def __init__(self, devices: DeviceManager, settings: config.Settings):
        super().__init__()
        self._settings = settings
        self._devices = devices
        self._lighting: dict[str, DeviceLighting] = {}
        self._dim = 1.0
        devices.connect("device-added", self._on_device_added)
        devices.connect("changed", self._on_devices_changed)
        for device in devices.devices:
            self._on_device_added(devices, device)

    def _on_device_added(self, manager, device: Device):
        lighting = DeviceLighting(device, self._settings.device(device.settings_key))
        lighting.set_dim(self._dim)
        self._lighting[device.key] = lighting
        lighting.apply()

    def _on_devices_changed(self, manager):
        present = {d.key for d in manager.devices}
        for key in list(self._lighting):
            if key not in present:
                self._lighting.pop(key).stop()

    def get(self, key: str) -> DeviceLighting | None:
        return self._lighting.get(key)

    @property
    def dim(self) -> float:
        return self._dim

    def set_dim(self, factor: float):
        self._dim = factor
        for lighting in self._lighting.values():
            lighting.set_dim(factor)
