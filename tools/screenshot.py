"""Renders the main window in demo mode to a PNG, for the AppStream screenshots.

Usage: SYNAPTIX_DEMO=1 python3 tools/screenshot.py OUTPUT.png [light|dark] [DEVICE_INDEX] [EFFECT]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402

from synaptix import i18n  # noqa: E402
from synaptix.app import SynaptixApplication  # noqa: E402

i18n.setup(os.environ.get("SYNAPTIX_LOCALEDIR", ""))

output = sys.argv[1]
scheme = sys.argv[2] if len(sys.argv) > 2 else "light"
device_index = int(sys.argv[3]) if len(sys.argv) > 3 else 0
effect = sys.argv[4] if len(sys.argv) > 4 else None


class ScreenshotApp(SynaptixApplication):
    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if scheme == "dark" else Adw.ColorScheme.FORCE_LIGHT)
        window = self._ensure_window()
        window.set_default_size(1000, 860)
        window.present()
        GLib.timeout_add(800, self._select, window)
        GLib.timeout_add(2000, self._capture, window)

    def _select(self, window):
        row = window._list.get_row_at_index(device_index)
        if row:
            window._list.select_row(row)
        if effect:
            page = window._device_page
            page._effect_row.set_selected(page._effects.index(effect))
        if os.environ.get("SYNAPTIX_SCREENSHOT_PAINT"):
            GLib.timeout_add(500, self._paint_some, window)
        return GLib.SOURCE_REMOVE

    def _paint_some(self, window):
        """Paints a few keys through the preview, exercising click-to-paint."""
        page = window._device_page
        brush = page._led_editor._brush
        for color, count in (("#ff2a6d", 30), ("#05d9e8", 30), ("#d1f7ff", 20)):
            rgba = brush.get_rgba()
            rgba.parse(color)
            brush.set_rgba(rgba)
            areas = page._preview._hit_areas
            start = len(areas) // 2 if color == "#05d9e8" else (0 if color == "#ff2a6d" else len(areas) - count)
            for x, y, w, h, _index in areas[start:start + count]:
                page._preview._on_pressed(None, 1, x + w / 2, y + h / 2)
        return GLib.SOURCE_REMOVE

    def _capture(self, window):
        paintable = Gtk.WidgetPaintable.new(window)
        width, height = window.get_width(), window.get_height()
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        renderer = window.get_native().get_renderer()
        texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
        texture.save_to_png(output)
        self.quit_for_real()
        return GLib.SOURCE_REMOVE


sys.exit(ScreenshotApp().run([sys.argv[0]]))
