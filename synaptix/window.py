import math

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import config
from .controller import BlockedDevice, Device
from .i18n import _
from .preview import DevicePreview
from .razer import catalog
from .razer.driver import SPEED_FAST, SPEED_MEDIUM, SPEED_SLOW

UDEV_RULE = 'KERNEL=="hidraw*", ATTRS{idVendor}=="1532", TAG+="uaccess"'
UDEV_COMMAND = (
    f"echo '{UDEV_RULE}' | sudo tee /etc/udev/rules.d/70-synaptix-razer.rules"
    " && sudo udevadm control --reload"
    " && sudo udevadm trigger --subsystem-match=hidraw"
)

COMMIT_DELAY_MS = 800

_TYPE_ICONS = {
    "keyboard": "input-keyboard-symbolic",
    "laptop": "computer-symbolic",
    "mouse": "input-mouse-symbolic",
    "headset": "audio-headset-symbolic",
    "headset_stand": "audio-headset-symbolic",
    "speaker": "audio-speakers-symbolic",
    "microphone": "audio-input-microphone-symbolic",
    "keypad": "input-gaming-symbolic",
}


def _effect_labels():
    return {
        catalog.STATIC: _("Static"),
        catalog.BREATHING: _("Breathing"),
        catalog.BREATHING_DUAL: _("Breathing (Two Colors)"),
        catalog.BREATHING_RANDOM: _("Breathing (Random)"),
        config.PULSE: _("Pulse"),
        catalog.SPECTRUM: _("Spectrum Cycle"),
        catalog.WAVE: _("Wave"),
        catalog.REACTIVE: _("Reactive"),
        catalog.RIPPLE: _("Ripple"),
        catalog.STARLIGHT: _("Starlight"),
        catalog.FIRE: _("Fire"),
        catalog.CUSTOM: _("Per-LED Colors"),
        catalog.OFF: _("Off"),
    }


_USES_COLOR = {catalog.STATIC, catalog.BREATHING, catalog.BREATHING_DUAL, config.PULSE,
               catalog.REACTIVE, catalog.RIPPLE, catalog.STARLIGHT}
_USES_SPEED = {catalog.REACTIVE, catalog.STARLIGHT}
_SPEEDS = (SPEED_FAST, SPEED_MEDIUM, SPEED_SLOW)


def _rgba(text: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.parse(text)
    return rgba


def _hex(rgba: Gdk.RGBA) -> str:
    return config.format_color((round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)))


class _LedSwatch(Gtk.DrawingArea):
    def __init__(self, size: int):
        super().__init__(content_width=size, content_height=size)
        self.color = (0.0, 0.0, 0.0)
        self.set_draw_func(self._draw)

    def set_color(self, rgb):
        self.color = rgb
        self.queue_draw()

    def _draw(self, area, cr, width, height):
        cr.arc(width / 2, height / 2, min(width, height) / 2 - 1, 0, 2 * math.pi)
        cr.set_source_rgb(*self.color)
        cr.fill_preserve()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.6)
        cr.set_line_width(1)
        cr.stroke()


class LedEditor(Gtk.Box):
    """Paints individual LEDs, grouped by zone."""

    def __init__(self, on_changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._on_changed = on_changed
        self._lighting = None
        self._swatches: list[_LedSwatch] = []

        toolbar = Gtk.Box(spacing=12)
        toolbar.append(Gtk.Label(label=_("Brush"), css_classes=["dim-label"]))
        self._brush = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                            rgba=_rgba(config.RAZER_GREEN), valign=Gtk.Align.CENTER)
        self._brush.update_property([Gtk.AccessibleProperty.LABEL], [_("Brush")])
        toolbar.append(self._brush)
        fill = Gtk.Button(label=_("Fill All"), css_classes=["flat"])
        fill.connect("clicked", self._on_fill)
        toolbar.append(fill)
        self.append(toolbar)

        self._zones = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroller = Gtk.ScrolledWindow(child=self._zones, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True)
        self.append(scroller)

    def show_device(self, lighting):
        self._lighting = lighting
        while child := self._zones.get_first_child():
            self._zones.remove(child)
        self._swatches = []
        colors = lighting.custom_colors()
        size = 14 if lighting.device.info.led_count > 40 else 26
        index = 0
        for zone in lighting.device.info.zones:
            self._zones.append(Gtk.Label(label=zone.name, xalign=0, css_classes=["heading"]))
            grid = Gtk.Grid(row_spacing=2, column_spacing=2)
            for row in range(zone.rows):
                for col in range(zone.cols):
                    swatch = _LedSwatch(size)
                    swatch.set_color(tuple(v / 255 for v in colors[index]))
                    button = Gtk.Button(child=swatch, css_classes=["flat", "led-button"])
                    button.update_property([Gtk.AccessibleProperty.LABEL],
                                           [_("{zone} LED {number}").format(zone=zone.name, number=index + 1)])
                    button.connect("clicked", self._on_led_clicked, index)
                    grid.attach(button, col, row, 1, 1)
                    self._swatches.append(swatch)
                    index += 1
            self._zones.append(grid)

    def paint(self, indexes):
        colors = [config.format_color(c) for c in self._lighting.custom_colors()]
        brush = _hex(self._brush.get_rgba())
        for i in indexes:
            colors[i] = brush
            self._swatches[i].set_color(tuple(v / 255 for v in config.parse_color(brush)))
        self._lighting.settings.custom = colors
        self._on_changed()

    def _on_led_clicked(self, _button, index):
        self.paint([index])

    def _on_fill(self, _button):
        self.paint(range(len(self._swatches)))


class DevicePage(Adw.PreferencesPage):
    def __init__(self, window):
        super().__init__()
        self._window = window
        self._lighting = None
        self._effects: list[str] = []
        self._updating = False

        preview_group = Adw.PreferencesGroup()
        self._preview = DevicePreview()
        self._preview.update_property([Gtk.AccessibleProperty.LABEL], [_("Lighting preview")])
        for side in ("top", "bottom", "start", "end"):
            getattr(self._preview, f"set_margin_{side}")(12)
        preview_group.add(Gtk.Frame(child=self._preview, css_classes=["card"]))
        self._info_label = Gtk.Label(css_classes=["dim-label", "caption"], margin_top=8, wrap=True,
                                     justify=Gtk.Justification.CENTER)
        preview_group.add(self._info_label)
        self.add(preview_group)

        group = Adw.PreferencesGroup(title=_("Lighting"))
        self._effect_row = Adw.ComboRow(title=_("Effect"))
        self._effect_row.connect("notify::selected", self._on_effect_selected)
        group.add(self._effect_row)

        self._color_row, self._color_button = self._color_row_new(_("Color"), 1)
        group.add(self._color_row)
        self._color2_row, self._color2_button = self._color_row_new(_("Second Color"), 2)
        group.add(self._color2_row)

        self._starlight_row = Adw.ComboRow(title=_("Colors"), model=Gtk.StringList.new(
            [_("Random"), _("One Color"), _("Two Colors")]))
        self._starlight_row.connect("notify::selected", self._on_starlight_selected)
        group.add(self._starlight_row)

        self._speed_row = Adw.ComboRow(title=_("Speed"), model=Gtk.StringList.new([_("Fast"), _("Medium"), _("Slow")]))
        self._speed_row.connect("notify::selected", self._on_speed_selected)
        group.add(self._speed_row)

        self._direction_row = Adw.ComboRow(title=_("Direction"),
                                           model=Gtk.StringList.new([_("Left"), _("Right")]))
        self._direction_row.connect("notify::selected", self._on_direction_selected)
        group.add(self._direction_row)

        self._pulse_row = Adw.SpinRow.new_with_range(1.0, 10.0, 0.5)
        self._pulse_row.set_title(_("Pulse Duration"))
        self._pulse_row.set_subtitle(_("Seconds per pulse"))
        self._pulse_row.set_digits(1)
        self._pulse_row.connect("notify::value", self._on_pulse_changed)
        group.add(self._pulse_row)

        self._brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self._brightness.set_hexpand(True)
        self._brightness.set_valign(Gtk.Align.CENTER)
        self._brightness.set_size_request(200, -1)
        self._brightness.set_draw_value(True)
        self._brightness.set_value_pos(Gtk.PositionType.RIGHT)
        self._brightness.set_format_value_func(lambda scale, value: f"{value:.0f}%")
        self._brightness.update_property([Gtk.AccessibleProperty.LABEL], [_("Brightness")])
        self._brightness.connect("value-changed", self._on_brightness_changed)
        self._brightness_row = Adw.ActionRow(title=_("Brightness"))
        self._brightness_row.add_suffix(self._brightness)
        group.add(self._brightness_row)

        self._dim_row = Adw.SwitchRow(title=_("Dim When Idle"),
                                      subtitle=_("Include this device in idle dimming"))
        self._dim_row.connect("notify::active", self._on_dim_toggled)
        group.add(self._dim_row)
        self.add(group)

        self._led_group = Adw.PreferencesGroup(
            title=_("LEDs"), description=_("Pick a brush color, then click LEDs here or in the preview to paint them"))
        self._led_editor = LedEditor(self._on_leds_changed)
        self._led_group.add(self._led_editor)
        self.add(self._led_group)

    def _color_row_new(self, title, which):
        button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        button.update_property([Gtk.AccessibleProperty.LABEL], [title])
        button.connect("notify::rgba", self._on_color_changed, which)
        row = Adw.ActionRow(title=title)
        row.add_suffix(button)
        row.set_activatable_widget(button)
        return row, button

    @property
    def lighting(self):
        return self._lighting

    def show_device(self, device: Device, lighting):
        self._lighting = lighting
        self._updating = True
        info = device.info
        s = lighting.settings
        details = [info.name]
        if device.via:
            details.append(_("through {receiver}").format(receiver=device.via))
        if device.firmware:
            details.append(_("Firmware {version}").format(version=device.firmware))
        if device.serial:
            details.append(device.serial)
        if not info.verified:
            details.append(_("Unknown model: effects detected from the firmware"))
        self._info_label.set_label(" · ".join(details))
        self._preview.show_device(info, lighting.preview_colors)

        labels = _effect_labels()
        self._effects = lighting.effects()
        self._effect_row.set_model(Gtk.StringList.new([labels.get(e, e) for e in self._effects]))
        if s.effect in self._effects:
            self._effect_row.set_selected(self._effects.index(s.effect))
        self._color_button.set_rgba(_rgba(s.color))
        self._color2_button.set_rgba(_rgba(s.color2))
        self._starlight_row.set_selected(min(2, max(0, s.starlight_colors)))
        self._speed_row.set_selected(_SPEEDS.index(s.speed) if s.speed in _SPEEDS else 1)
        self._direction_row.set_selected(0 if s.direction == "left" else 1)
        self._pulse_row.set_value(s.pulse_period)
        self._brightness.set_value(s.brightness)
        self._dim_row.set_active(s.dim_when_idle)
        if catalog.CUSTOM in self._effects:
            self._led_editor.show_device(lighting)
        self._updating = False
        self._sync_rows()

    def _sync_rows(self):
        lighting = self._lighting
        effect = lighting.settings.effect
        mono = lighting.device.info.monochrome
        starlight_colors = lighting.settings.starlight_colors
        uses_color = effect in _USES_COLOR and not mono
        if effect == catalog.STARLIGHT:
            uses_color = starlight_colors >= 1
        self._color_row.set_visible(uses_color)
        self._color2_row.set_visible(not mono and (effect == catalog.BREATHING_DUAL
                                                   or (effect == catalog.STARLIGHT and starlight_colors == 2)))
        self._starlight_row.set_visible(effect == catalog.STARLIGHT)
        self._speed_row.set_visible(effect in _USES_SPEED)
        self._direction_row.set_visible(effect == catalog.WAVE)
        self._pulse_row.set_visible(effect == config.PULSE)
        self._brightness_row.set_visible(lighting.device.supports_brightness)
        self._brightness_row.set_sensitive(effect != catalog.OFF)
        self._led_group.set_visible(effect == catalog.CUSTOM)
        self._preview.set_led_click_handler(
            (lambda index: self._led_editor.paint([index])) if effect == catalog.CUSTOM else None)

    def _changed(self, resend=True):
        if resend:
            self._lighting.refresh_effect()
        self._sync_rows()
        self._window.schedule_commit(self._lighting)

    def _on_effect_selected(self, row, _pspec):
        index = row.get_selected()
        if self._updating or index >= len(self._effects):
            return
        self._lighting.set_effect(self._effects[index])
        if self._effects[index] == catalog.CUSTOM:
            self._led_editor.show_device(self._lighting)
        self._changed(resend=False)

    def _on_color_changed(self, button, _pspec, which):
        if self._updating:
            return
        value = _hex(button.get_rgba())
        if which == 1:
            self._lighting.settings.color = value
        else:
            self._lighting.settings.color2 = value
        self._changed()

    def _on_starlight_selected(self, row, _pspec):
        if not self._updating:
            self._lighting.settings.starlight_colors = row.get_selected()
            self._changed()

    def _on_speed_selected(self, row, _pspec):
        if not self._updating:
            self._lighting.settings.speed = _SPEEDS[row.get_selected()]
            self._changed()

    def _on_direction_selected(self, row, _pspec):
        if not self._updating:
            self._lighting.settings.direction = "left" if row.get_selected() == 0 else "right"
            self._changed()

    def _on_pulse_changed(self, row, _pspec):
        if not self._updating:
            self._lighting.settings.pulse_period = row.get_value()
            self._changed(resend=False)

    def _on_brightness_changed(self, scale):
        if not self._updating:
            self._lighting.set_brightness(round(scale.get_value()))
            self._changed(resend=False)

    def _on_dim_toggled(self, row, _pspec):
        if not self._updating:
            self._lighting.settings.dim_when_idle = row.get_active()
            self._changed(resend=False)

    def _on_leds_changed(self):
        self._changed()


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title=_("Preferences"))
        self._app = app
        s = app.settings
        page = Adw.PreferencesPage()

        idle = Adw.PreferencesGroup(title=_("Idle Dimming"))
        self._idle_row = Adw.ExpanderRow(title=_("Dim When Idle"), show_enable_switch=True,
                                         subtitle=_("Turn the lighting off after a period without input"),
                                         enable_expansion=s.idle_enabled)
        self._idle_row.connect("notify::enable-expansion", self._on_changed)
        self._timeout_row = Adw.SpinRow.new_with_range(10, 3600, 10)
        self._timeout_row.set_title(_("Idle Time"))
        self._timeout_row.set_subtitle(_("Seconds before dimming"))
        self._timeout_row.set_value(s.idle_timeout)
        self._timeout_row.connect("notify::value", self._on_changed)
        self._idle_row.add_row(self._timeout_row)
        self._breaths_row = Adw.SpinRow.new_with_range(0, 10, 1)
        self._breaths_row.set_title(_("Warning Breaths"))
        self._breaths_row.set_subtitle(_("Pulses before the light goes out"))
        self._breaths_row.set_value(s.idle_warning_breaths)
        self._breaths_row.connect("notify::value", self._on_changed)
        self._idle_row.add_row(self._breaths_row)
        self._fade_in_row = Adw.SwitchRow(title=_("Fade In"), subtitle=_("Light up smoothly when you return"),
                                          active=s.idle_fade_in)
        self._fade_in_row.connect("notify::active", self._on_changed)
        self._idle_row.add_row(self._fade_in_row)
        if not app.dimmer.available:
            self._idle_row.set_subtitle(_("Not available: your desktop does not report idle time"))
            self._idle_row.set_sensitive(False)
        idle.add(self._idle_row)
        page.add(idle)

        behavior = Adw.PreferencesGroup(title=_("Behavior"))
        self._background_row = Adw.SwitchRow(
            title=_("Run in Background"),
            subtitle=_("Keep idle dimming and effects running after the window is closed"),
            active=s.run_in_background)
        self._background_row.connect("notify::active", self._on_background_toggled)
        behavior.add(self._background_row)
        self._autostart_row = Adw.SwitchRow(title=_("Start on Login"),
                                            subtitle=_("Apply your lighting when you log in"),
                                            active=s.autostart, sensitive=s.run_in_background)
        self._autostart_row.connect("notify::active", self._on_autostart_toggled)
        behavior.add(self._autostart_row)
        page.add(behavior)
        self.add(page)

    def _on_changed(self, *_args):
        s = self._app.settings
        s.idle_enabled = self._idle_row.get_enable_expansion()
        s.idle_timeout = int(self._timeout_row.get_value())
        s.idle_warning_breaths = int(self._breaths_row.get_value())
        s.idle_fade_in = self._fade_in_row.get_active()
        self._app.dimmer.settings_changed()
        self._app.save_settings()

    def _on_background_toggled(self, row, _pspec):
        self._app.settings.run_in_background = row.get_active()
        self._autostart_row.set_sensitive(row.get_active())
        if not row.get_active():
            self._autostart_row.set_active(False)
        self._app.save_settings()

    def _on_autostart_toggled(self, row, _pspec):
        if self._app.settings.autostart == row.get_active():
            return
        self._app.settings.autostart = row.get_active()
        self._app.save_settings()
        self._app.request_background(self._on_autostart_result)

    def _on_autostart_result(self, _background, autostart):
        if self._app.settings.autostart and not autostart:
            self._app.settings.autostart = False
            self._autostart_row.set_active(False)
            self._app.save_settings()
            self.add_toast(Adw.Toast(title=_("Starting on login was not allowed")))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=_("Synaptix"))
        self._app = app
        self._commit_ids: dict[str, int] = {}
        self._selected_key: str | None = None
        self._row_keys: list[str] = []
        self.set_default_size(980, 820)
        self.set_size_request(360, 400)

        # Sidebar
        self._list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self._list.connect("row-selected", self._on_row_selected)
        sidebar_header = Adw.HeaderBar()
        sidebar_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text=_("Main Menu"),
                                               menu_model=self._build_menu(), primary=True))
        sidebar_view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self._list))
        sidebar_view.add_top_bar(sidebar_header)
        sidebar = Adw.NavigationPage(title=_("Devices"), child=sidebar_view)

        # Content
        self._device_page = DevicePage(self)
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._build_empty_page(), "empty")
        self._stack.add_named(self._device_page, "device")
        self._stack.add_named(self._build_permission_page(), "permission")
        self._unsupported_page = Adw.StatusPage(icon_name="dialog-information-symbolic",
                                                title=_("Not Supported Yet"))
        self._stack.add_named(self._unsupported_page, "unsupported")
        self._toasts = Adw.ToastOverlay(child=self._stack)
        self._content_title = Adw.WindowTitle(title=_("Synaptix"))
        content_view = Adw.ToolbarView(content=self._toasts)
        content_view.add_top_bar(Adw.HeaderBar(title_widget=self._content_title))
        self._content = Adw.NavigationPage(title=_("Synaptix"), child=content_view)

        self._split = Adw.NavigationSplitView(sidebar=sidebar, content=self._content,
                                              min_sidebar_width=220, max_sidebar_width=300)
        self.set_content(self._split)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        breakpoint.add_setter(self._split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        app.devices.connect("changed", lambda *_: self._rebuild_list())
        self._rebuild_list()

    @staticmethod
    def _build_menu():
        menu = Gio.Menu()
        menu.append(_("Preferences"), "app.preferences")
        menu.append(_("About Synaptix"), "app.about")
        menu.append(_("Quit"), "app.quit")
        return menu

    def _build_empty_page(self):
        return Adw.StatusPage(
            icon_name="input-keyboard-symbolic",
            title=_("No Razer Devices Found"),
            description=_("Connect a Razer keyboard, mouse, mouse mat or accessory. "
                          "{count} models are known, and other models are detected automatically.").format(
                count=catalog.supported_count()),
        )

    def _build_permission_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, halign=Gtk.Align.CENTER)
        command = Gtk.Label(label=UDEV_COMMAND, wrap=True, wrap_mode=Pango.WrapMode.CHAR, selectable=True,
                            xalign=0, css_classes=["monospace"], max_width_chars=60)
        for side in ("top", "bottom", "start", "end"):
            getattr(command, f"set_margin_{side}")(12)
        box.append(Gtk.Frame(child=command, css_classes=["view"]))
        copy = Gtk.Button(label=_("Copy Command"), halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        copy.connect("clicked", self._on_copy_command)
        box.append(copy)
        return Adw.StatusPage(
            icon_name="dialog-password-symbolic",
            title=_("Permission Needed"),
            description=_("This device was found, but Synaptix is not allowed to talk to it yet. "
                          "Run this command once in a terminal; it installs a udev rule that gives "
                          "your user access to Razer devices. Synaptix will connect automatically."),
            child=box,
        )

    # Device list

    def _rebuild_list(self):
        manager = self._app.devices
        entries = [(d.key, d.name, _TYPE_ICONS.get(d.info.type, "applications-graphics-symbolic"), None)
                   for d in sorted(manager.devices, key=lambda d: d.name.lower())]
        for blocked in manager.blocked:
            subtitle = _("Permission needed") if blocked.reason == BlockedDevice.PERMISSION else _("Not supported")
            entries.append((blocked.key, blocked.name, "dialog-warning-symbolic", subtitle))

        self._row_keys = []
        while row := self._list.get_row_at_index(0):
            self._list.remove(row)
        for key, name, icon, subtitle in entries:
            row = Adw.ActionRow(title=name, subtitle=subtitle or "", activatable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            self._list.append(row)
            self._row_keys.append(key)

        if not entries:
            self._selected_key = None
            self._show_content("empty", _("Synaptix"))
            return
        index = self._row_keys.index(self._selected_key) if self._selected_key in self._row_keys else 0
        self._list.select_row(self._list.get_row_at_index(index))

    def _on_row_selected(self, _listbox, row):
        if row is None:
            return
        key = self._row_keys[row.get_index()]
        self._selected_key = key
        manager = self._app.devices
        device = manager.find(key)
        if device:
            lighting = self._app.lighting.get(key)
            if lighting:
                self._device_page.show_device(device, lighting)
            self._show_content("device", device.name)
        else:
            blocked = next((b for b in manager.blocked if b.key == key), None)
            if blocked and blocked.reason == BlockedDevice.PERMISSION:
                self._show_content("permission", blocked.name)
            elif blocked:
                self._unsupported_page.set_description(
                    _("{name} uses a lighting protocol Synaptix does not speak yet.").format(name=blocked.name))
                self._show_content("unsupported", blocked.name)
        self._split.set_show_content(True)

    def _show_content(self, page, title):
        self._stack.set_visible_child_name(page)
        self._content_title.set_title(title)
        self._content.set_title(title)

    # Persistence

    def schedule_commit(self, lighting):
        """Saves settings, and stores them in the device memory once the user stops changing them."""
        key = lighting.device.key
        if key in self._commit_ids:
            GLib.source_remove(self._commit_ids[key])
        self._commit_ids[key] = GLib.timeout_add(COMMIT_DELAY_MS, self._commit, lighting)

    def _commit(self, lighting):
        self._commit_ids.pop(lighting.device.key, None)
        self._app.save_settings()
        if lighting.settings.effect != config.PULSE and self._app.lighting.dim == 1.0:
            lighting.apply(save=True)
        return GLib.SOURCE_REMOVE

    def flush(self):
        for key, source in list(self._commit_ids.items()):
            GLib.source_remove(source)
        self._commit_ids.clear()
        self._app.save_settings()

    def _on_copy_command(self, _button):
        Gdk.Display.get_default().get_clipboard().set(UDEV_COMMAND)
        self._toasts.add_toast(Adw.Toast(title=_("Command copied")))
