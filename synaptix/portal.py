"""Background/autostart permission through the XDG desktop portal."""

import logging
import secrets

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

_BUS_NAME = "org.freedesktop.portal.Desktop"
_OBJECT_PATH = "/org/freedesktop/portal/desktop"


def request_background(reason: str, autostart: bool, commandline: list[str], callback):
    """Calls callback(granted_background, granted_autostart) once the user answered."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    token = "synaptix_" + secrets.token_hex(4)
    sender = bus.get_unique_name()[1:].replace(".", "_")
    handle = f"{_OBJECT_PATH}/request/{sender}/{token}"
    subscription = 0

    def on_response(connection, sender_name, path, iface, signal, params):
        bus.signal_unsubscribe(subscription)
        response, results = params.unpack()
        callback(response == 0 and results.get("background", False),
                 response == 0 and results.get("autostart", False))

    subscription = bus.signal_subscribe(
        _BUS_NAME, "org.freedesktop.portal.Request", "Response", handle,
        None, Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)

    options = {
        "handle_token": GLib.Variant("s", token),
        "reason": GLib.Variant("s", reason),
        "autostart": GLib.Variant("b", autostart),
        "commandline": GLib.Variant("as", commandline),
        "dbus-activatable": GLib.Variant("b", False),
    }

    def on_called(connection, result):
        try:
            connection.call_finish(result)
        except GLib.Error as e:
            log.warning("background portal unavailable: %s", e.message)
            bus.signal_unsubscribe(subscription)
            callback(False, False)

    bus.call(_BUS_NAME, _OBJECT_PATH, "org.freedesktop.portal.Background", "RequestBackground",
             GLib.Variant("(sa{sv})", ("", options)), None, Gio.DBusCallFlags.NONE, -1, None, on_called)
