import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import config, portal  # noqa: E402
from .controller import DeviceManager  # noqa: E402
from .i18n import _  # noqa: E402
from .idle import IdleDimmer  # noqa: E402
from .lighting import LightingManager  # noqa: E402

APP_ID = "io.github.joaovmourasouza.Synaptix"
VERSION = "0.1.0"


class SynaptixApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Start without opening a window"), None)
        self.settings: config.Settings | None = None
        self.devices: DeviceManager | None = None
        self.lighting: LightingManager | None = None
        self.dimmer: IdleDimmer | None = None
        self._window = None
        self._held = False
        self._start_hidden = False

    def do_handle_local_options(self, options):
        if options.contains("background"):
            self._start_hidden = True
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(".led-button { padding: 2px; min-width: 0; min-height: 0; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.settings = config.load()
        self.devices = DeviceManager()
        self.lighting = LightingManager(self.devices, self.settings)
        self.dimmer = IdleDimmer(self.lighting, self.settings)

        for name, callback, accels in (
            ("preferences", self._on_preferences, ["<Control>comma"]),
            ("about", self._on_about, []),
            ("quit", lambda *_: self.quit_for_real(), ["<Control>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self):
        if self._start_hidden:
            # Only the first activation (from autostart) stays hidden
            self._start_hidden = False
            self._hold_in_background()
            return
        self._ensure_window().present()
        if not self.settings.autostart_requested:
            GLib.idle_add(self._request_autostart_on_first_run)

    def _request_autostart_on_first_run(self):
        """Asks once to start on login, so lighting and idle dimming survive a reboot."""
        self.settings.autostart_requested = True
        self.settings.autostart = True
        self.save_settings()

        def on_result(_background, autostart):
            if not autostart:
                self.settings.autostart = False
                self.save_settings()

        self.request_background(on_result)
        return GLib.SOURCE_REMOVE

    def _ensure_window(self):
        if self._window is None:
            from .window import MainWindow
            self._window = MainWindow(self)
            self._window.connect("close-request", self._on_close_request)
        return self._window

    def _on_close_request(self, window):
        window.flush()
        if self.settings.run_in_background:
            self._hold_in_background()
            window.set_visible(False)
            return True
        self._release_hold()
        return False

    def _hold_in_background(self):
        if not self._held:
            self.hold()
            self._held = True
            self.request_background(None)

    def _release_hold(self):
        if self._held:
            self._held = False
            self.release()

    def request_background(self, callback):
        def on_result(background, autostart):
            if callback:
                callback(background, autostart)

        portal.request_background(
            _("Synaptix keeps your device lighting and idle dimming running."),
            self.settings.autostart, ["synaptix", "--background"], on_result)

    def save_settings(self):
        config.save(self.settings)

    def quit_for_real(self):
        if self._window:
            self._window.flush()
        self.devices.shutdown()
        self.quit()

    def _on_preferences(self, *_args):
        from .window import PreferencesDialog
        PreferencesDialog(self).present(self._ensure_window())

    def _on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_("Synaptix"),
            application_icon=APP_ID,
            developer_name="João Moura",
            version=VERSION,
            website="https://github.com/joaovmourasouza/Synaptix",
            issue_url="https://github.com/joaovmourasouza/Synaptix/issues",
            license_type=Gtk.License.GPL_2_0,
            comments=_("Lighting control for Razer peripherals, talking to the devices directly over USB HID. "
                       "Not affiliated with Razer Inc."),
        )
        about.add_credit_section(_("Device data and protocol"), [
            "OpenRGB https://openrgb.org",
            "OpenRazer https://openrazer.github.io",
        ])
        about.present(self._ensure_window())


def main(localedir: str | None = None) -> int:
    from . import i18n
    i18n.setup(localedir or "")
    logging.basicConfig(level=logging.INFO if "--verbose" in sys.argv else logging.WARNING,
                        format="%(name)s: %(message)s")
    argv = [a for a in sys.argv if a != "--verbose"]
    return SynaptixApplication().run(argv)
