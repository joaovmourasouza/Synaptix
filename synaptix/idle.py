"""Dims the keyboard when the session is idle.

Idle time comes from the desktop over D-Bus (GNOME Mutter or the
freedesktop ScreenSaver interface used by KDE Plasma), which works inside the
Flatpak sandbox without access to /dev/input.
"""

import logging
import math
import time

from gi.repository import Gio, GLib, GObject

from . import config
from .lighting import FRAME_MS, LightingManager

log = logging.getLogger(__name__)

ACTIVE_POLL_MS = 1000
IDLE_POLL_MS = 150
BREATH_SECONDS = 1.6
FADE_OUT_SECONDS = 1.2
FADE_IN_SECONDS = 0.8

# (bus name, object path, interface, method)
_PROVIDERS = (
    ("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
     "org.gnome.Mutter.IdleMonitor", "GetIdletime"),
    ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
     "org.freedesktop.ScreenSaver", "GetSessionIdleTime"),
)


class IdleTimeSource:
    """Asynchronously reads the session idle time in milliseconds."""

    def __init__(self):
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self._providers = list(_PROVIDERS)
        self.available = True

    def query(self, callback):
        if not self._providers:
            self.available = False
            callback(None)
            return
        name, path, iface, method = self._providers[0]

        def on_done(bus, result):
            try:
                value = bus.call_finish(result).unpack()[0]
            except GLib.Error as e:
                log.info("idle provider %s unavailable: %s", name, e.message)
                self._providers.pop(0)
                self.query(callback)
                return
            callback(int(value))

        self._bus.call(name, path, iface, method, None, None,
                       Gio.DBusCallFlags.NONE, 1000, None, on_done)


ACTIVE, WARNING, FADE_OUT, OFF, FADE_IN = range(5)


class IdleDimmer(GObject.Object):
    __gsignals__ = {
        "availability-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, engine: LightingManager, settings: config.Settings):
        super().__init__()
        self._engine = engine
        self._settings = settings
        self._source = IdleTimeSource()
        self._state = ACTIVE
        self._last_idle_ms = 0
        self._phase_start = 0.0
        self._phase_from = 1.0
        self._poll_id = 0
        self._frame_id = 0
        self._schedule_poll(ACTIVE_POLL_MS)

    @property
    def available(self) -> bool:
        return self._source.available

    def settings_changed(self):
        if not self._settings.idle_enabled and self._state != ACTIVE:
            self._start_fade_in()

    def _schedule_poll(self, delay_ms: int):
        if self._poll_id:
            GLib.source_remove(self._poll_id)
        self._poll_id = GLib.timeout_add(delay_ms, self._poll)

    def _poll(self):
        self._poll_id = 0
        self._source.query(self._on_idle_time)
        return GLib.SOURCE_REMOVE

    def _on_idle_time(self, idle_ms):
        if idle_ms is None:
            self.emit("availability-changed")
            return
        user_returned = idle_ms < self._last_idle_ms or idle_ms < IDLE_POLL_MS * 4
        self._last_idle_ms = idle_ms

        if self._state == ACTIVE:
            if self._settings.idle_enabled and idle_ms >= self._settings.idle_timeout * 1000:
                if self._settings.idle_warning_breaths > 0:
                    self._enter(WARNING)
                else:
                    self._enter(FADE_OUT)
        elif self._state in (WARNING, FADE_OUT, OFF) and user_returned:
            self._start_fade_in()

        self._schedule_poll(ACTIVE_POLL_MS if self._state == ACTIVE else IDLE_POLL_MS)

    def _start_fade_in(self):
        if self._settings.idle_fade_in:
            self._enter(FADE_IN)
        else:
            self._enter(ACTIVE)
            self._engine.set_dim(1.0)

    def _enter(self, state: int):
        self._state = state
        self._phase_start = time.monotonic()
        self._phase_from = self._engine.dim
        if state in (WARNING, FADE_OUT, FADE_IN) and not self._frame_id:
            self._frame_id = GLib.timeout_add(FRAME_MS, self._on_frame)

    def _on_frame(self):
        elapsed = time.monotonic() - self._phase_start
        state = self._state
        if state == WARNING:
            total = BREATH_SECONDS * self._settings.idle_warning_breaths
            if elapsed >= total:
                self._enter(FADE_OUT)
                return GLib.SOURCE_CONTINUE
            self._engine.set_dim(0.5 + 0.5 * math.cos(2 * math.pi * elapsed / BREATH_SECONDS))
        elif state == FADE_OUT:
            progress = min(1.0, elapsed / FADE_OUT_SECONDS)
            self._engine.set_dim(self._phase_from * (1 - progress))
            if progress >= 1.0:
                self._state = OFF
        elif state == FADE_IN:
            progress = min(1.0, elapsed / FADE_IN_SECONDS)
            self._engine.set_dim(self._phase_from + (1 - self._phase_from) * progress)
            if progress >= 1.0:
                self._state = ACTIVE
        if self._state in (ACTIVE, OFF):
            self._frame_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
