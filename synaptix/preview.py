"""Animated drawing of a device reflecting its current lighting."""

import math

import cairo
import gi

gi.require_version("PangoCairo", "1.0")

from gi.repository import Gtk, Pango, PangoCairo  # noqa: E402

# Keyboard rows: (legend, width in key units); a None legend is a gap.
_MAIN = [
    [("Esc", 1), (None, 1), ("F1", 1), ("F2", 1), ("F3", 1), ("F4", 1), (None, 0.5),
     ("F5", 1), ("F6", 1), ("F7", 1), ("F8", 1), (None, 0.5),
     ("F9", 1), ("F10", 1), ("F11", 1), ("F12", 1)],
    [("`", 1)] + [(c, 1) for c in "1234567890-="] + [("⌫", 2)],
    [("Tab", 1.5)] + [(c, 1) for c in "QWERTYUIOP[]"] + [("\\", 1.5)],
    [("Caps", 1.75)] + [(c, 1) for c in "ASDFGHJKL;'"] + [("Enter", 2.25)],
    [("Shift", 2.25)] + [(c, 1) for c in "ZXCVBNM,./"] + [("Shift", 2.75)],
    [("Ctrl", 1.25), ("❖", 1.25), ("Alt", 1.25), ("", 6.25), ("Alt", 1.25),
     ("Fn", 1.25), ("☰", 1.25), ("Ctrl", 1.25)],
]
_NAV = [
    [("Prt", 1), ("Scr", 1), ("Pau", 1)],
    [("Ins", 1), ("Hm", 1), ("PU", 1)],
    [("Del", 1), ("End", 1), ("PD", 1)],
    [],
    [(None, 1), ("↑", 1)],
    [("←", 1), ("↓", 1), ("→", 1)],
]
_NUMPAD = [
    [],
    [("Num", 1), ("/", 1), ("*", 1), ("-", 1)],
    [("7", 1), ("8", 1), ("9", 1), ("+", 1)],
    [("4", 1), ("5", 1), ("6", 1)],
    [("1", 1), ("2", 1), ("3", 1), ("⏎", 1)],
    [("0", 2), (".", 1)],
]
_MAIN_WIDTH = 15
_GAP = 0.35
_FUNCTION_ROW_GAP = 0.4
_PADDING = 0.45
FRAME_US = 41_000  # about 24 frames per second


def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _keyboard_blocks(cols: int):
    """Blocks drawn for a keyboard whose LED matrix has `cols` columns."""
    blocks = [(_MAIN, 0)]
    if cols == 1 or cols >= 17:
        blocks.append((_NAV, _MAIN_WIDTH + _GAP))
    if cols >= 21:
        blocks.append((_NUMPAD, _MAIN_WIDTH + _GAP + 3 + _GAP))
    return blocks


class DevicePreview(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self._info = None
        self._colors_func = None
        self._on_led_clicked = None
        # (x, y, width, height, LED index) of everything drawn, for clicks
        self._hit_areas: list[tuple[float, float, float, float, int]] = []
        self._colors = None
        self._last_tick = 0
        self._layouts: dict[tuple[str, int], Pango.Layout] = {}
        self._keyboard_cache = None
        self.set_content_height(220)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._on_tick)
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)

    def show_device(self, info, colors_func):
        self._info = info
        self._colors_func = colors_func
        self._colors = None
        self._keyboard_cache = None
        self.queue_draw()

    def _on_tick(self, _widget, clock):
        # At most ~30 redraws per second, and only when some LED changed colour
        now = clock.get_frame_time()
        if self._colors_func is None or now - self._last_tick < FRAME_US:
            return True
        self._last_tick = now
        colors = self._colors_func()
        if colors != self._colors:
            self._colors = colors
            self.queue_draw()
        return True

    def set_led_click_handler(self, handler):
        """handler(led_index) is called when an LED is clicked; None disables painting."""
        self._on_led_clicked = handler
        self.set_cursor_from_name("crosshair" if handler else None)

    def _on_pressed(self, _gesture, _n_press, x, y):
        if not self._on_led_clicked:
            return
        for hx, hy, hw, hh, index in self._hit_areas:
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                self._on_led_clicked(index)
                return

    def _draw(self, area, cr, width, height):
        if self._info is None:
            return
        self._hit_areas = []
        colors = self._colors if self._colors is not None else self._colors_func()
        if self._info.type in ("keyboard", "laptop"):
            self._draw_keyboard(cr, width, height, colors)
        else:
            self._draw_zones(cr, width, height, colors)

    # Keyboards

    def _keyboard_layout(self, width, height):
        """Geometry of every key plus a pre-rendered unlit keyboard, cached per size."""
        info = self._info
        scale = self.get_scale_factor()
        cache_key = (width, height, scale, info.rows, info.cols)
        if self._keyboard_cache and self._keyboard_cache[0] == cache_key:
            return self._keyboard_cache[1]

        blocks = _keyboard_blocks(info.cols)
        width_units = max(offset + max(sum(w for _, w in row) for row in rows if row) for rows, offset in blocks)
        height_units = 6 + _FUNCTION_ROW_GAP
        unit = min(width / (width_units + 2 * _PADDING), height / (height_units + 2 * _PADDING))
        board_w = (width_units + 2 * _PADDING) * unit
        board_h = (height_units + 2 * _PADDING) * unit
        ox, oy = (width - board_w) / 2, (height - board_h) / 2
        led_count = max(1, info.rows * info.cols)

        def led_index(row, x_units):
            if led_count == 1:
                return 0
            matrix_row = min(info.rows - 1, round(row * (info.rows - 1) / 5)) if info.rows > 1 else 0
            col = min(info.cols - 1, int(x_units / width_units * info.cols))
            return matrix_row * info.cols + col

        base = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width * scale), int(height * scale))
        base.set_device_scale(scale, scale)
        bc = cairo.Context(base)
        _rounded_rect(bc, ox, oy, board_w, board_h, unit * 0.35)
        bc.set_source_rgb(0.09, 0.09, 0.10)
        bc.fill_preserve()
        bc.set_source_rgba(1, 1, 1, 0.06)
        bc.set_line_width(1)
        bc.stroke()

        keys = []
        x0, y0 = ox + _PADDING * unit, oy + _PADDING * unit
        gap = unit * 0.08
        for rows, offset in blocks:
            for row_index, row in enumerate(rows):
                y = y0 + row_index * unit + (_FUNCTION_ROW_GAP * unit if row_index > 0 else 0)
                x_units = offset
                for legend, w in row:
                    if legend is not None:
                        kx, ky, kw, kh = x0 + x_units * unit + gap, y + gap, w * unit - 2 * gap, unit - 2 * gap
                        _rounded_rect(bc, kx, ky, kw, kh, unit * 0.12)
                        bc.set_source_rgb(0.16, 0.16, 0.17)
                        bc.fill()
                        top = (kx + unit * 0.06, ky + unit * 0.04, kw - unit * 0.12, kh - unit * 0.14)
                        _rounded_rect(bc, *top, unit * 0.1)
                        bc.set_source_rgb(0.20, 0.20, 0.21)
                        bc.fill()
                        keys.append((legend, kx, ky, kw, kh, top, led_index(row_index, x_units + w / 2)))
                    x_units += w

        board = (ox + unit * 0.3, oy + unit * 0.3, board_w - unit * 0.6, board_h - unit * 0.6, unit * 0.3)
        layout = (base, keys, unit, board)
        self._keyboard_cache = (cache_key, layout)
        return layout

    def _draw_keyboard(self, cr, width, height, colors):
        base, keys, unit, board = self._keyboard_layout(width, height)
        cr.set_source_surface(base, 0, 0)
        cr.paint()

        average = tuple(sum(c[i] for c in colors) / len(colors) for i in range(3))
        if max(average) > 0:
            _rounded_rect(cr, *board)
            cr.set_source_rgba(*average, 0.10)
            cr.fill()

        for legend, kx, ky, kw, kh, top, index in keys:
            color = colors[min(index, len(colors) - 1)]
            self._hit_areas.append((kx, ky, kw, kh, index))
            if max(color) > 0:
                # Backlight shining through the keycap
                cr.rectangle(*top)
                cr.set_source_rgba(*color, 0.18)
                cr.fill()
            if legend:
                layout = self._layout(cr, legend, unit * (0.30 if len(legend) == 1 else 0.21))
                _ink, logical = layout.get_pixel_extents()
                # Unlit legends stay faintly visible like real keycaps
                cr.set_source_rgb(*(0.32 + 0.68 * c for c in color))
                cr.move_to(kx + (kw - logical.width) / 2 - logical.x,
                           ky + (kh - unit * 0.1 - logical.height) / 2 - logical.y)
                PangoCairo.show_layout(cr, layout)

    def _layout(self, cr, legend: str, size: float) -> Pango.Layout:
        key = (legend, round(size * 4))
        layout = self._layouts.get(key)
        if layout is None:
            layout = PangoCairo.create_layout(cr)
            font = Pango.FontDescription.from_string("Sans Bold")
            font.set_absolute_size(size * Pango.SCALE)
            layout.set_font_description(font)
            layout.set_text(legend, -1)
            self._layouts[key] = layout
        else:
            PangoCairo.update_layout(cr, layout)
        return layout

    # Other devices: one group of LEDs per zone

    def _draw_zones(self, cr, width, height, colors):
        zones = self._info.zones or ()
        if not zones:
            return
        label_h = 18
        spacing = 14
        row_units = sum(max(1, z.rows) for z in zones)
        max_cols = max(z.cols for z in zones)
        available_h = height - len(zones) * (label_h + spacing)
        cell = min(44, (width - 24) / max(1, max_cols), available_h / max(1, row_units))
        y = (height - (row_units * cell + len(zones) * (label_h + spacing))) / 2
        index = 0
        for zone in zones:
            layout = PangoCairo.create_layout(cr)
            layout.set_text(zone.name, -1)
            font = Pango.FontDescription.from_string("Sans 9")
            layout.set_font_description(font)
            _ink, logical = layout.get_pixel_extents()
            cr.set_source_rgba(0.5, 0.5, 0.5, 1)
            cr.move_to((width - logical.width) / 2, y)
            PangoCairo.show_layout(cr, layout)
            y += label_h
            zone_w = zone.cols * cell
            x0 = (width - zone_w) / 2
            for row in range(zone.rows):
                for col in range(zone.cols):
                    color = colors[index] if index < len(colors) else colors[-1]
                    cx, cy = x0 + col * cell + cell / 2, y + row * cell + cell / 2
                    radius = cell * 0.36
                    self._hit_areas.append((cx - cell / 2, cy - cell / 2, cell, cell, index))
                    index += 1
                    if max(color) > 0:
                        cr.arc(cx, cy, radius * 1.5, 0, 2 * math.pi)
                        cr.set_source_rgba(*color, 0.25)
                        cr.fill()
                    cr.arc(cx, cy, radius, 0, 2 * math.pi)
                    cr.set_source_rgb(*(0.12 + 0.88 * c for c in color))
                    cr.fill_preserve()
                    cr.set_source_rgba(1, 1, 1, 0.15)
                    cr.set_line_width(1)
                    cr.stroke()
            y += zone.rows * cell + spacing
