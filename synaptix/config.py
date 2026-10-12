"""Settings persisted as JSON in the user's config directory."""

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields

from gi.repository import GLib

log = logging.getLogger(__name__)

# Effect implemented by the app (brightness animation over a static colour)
PULSE = "pulse"

RAZER_GREEN = "#44d62c"


@dataclass
class DeviceSettings:
    effect: str = "static"
    brightness: int = 100  # percent
    color: str = RAZER_GREEN
    color2: str = "#9141ac"
    speed: int = 2  # 1 fast, 2 medium, 3 slow
    direction: str = "right"
    starlight_colors: int = 0  # 0 random, 1 or 2 colours
    pulse_period: float = 3.0  # seconds
    custom: list[str] = field(default_factory=list)  # one colour per LED
    dim_when_idle: bool = True


@dataclass
class Settings:
    idle_enabled: bool = True
    idle_timeout: int = 60  # seconds
    idle_warning_breaths: int = 3
    idle_fade_in: bool = True

    run_in_background: bool = True
    autostart: bool = False
    # Whether the first-run request to start on login was already made
    autostart_requested: bool = False

    devices: dict[str, DeviceSettings] = field(default_factory=dict)

    def device(self, key: str) -> DeviceSettings:
        if key not in self.devices:
            self.devices[key] = DeviceSettings()
        return self.devices[key]


def _path() -> str:
    name = "settings-demo.json" if os.environ.get("SYNAPTIX_DEMO") else "settings.json"
    return os.path.join(GLib.get_user_config_dir(), "synaptix", name)


def _from_dict(cls, data: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def load() -> Settings:
    try:
        with open(_path()) as f:
            data = json.load(f)
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable settings: %s", e)
        return Settings()
    devices = {key: _from_dict(DeviceSettings, value) for key, value in data.pop("devices", {}).items()
               if isinstance(value, dict)}
    settings = _from_dict(Settings, data)
    settings.devices = devices
    return settings


def save(settings: Settings):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(settings), f, indent=2)
    os.replace(tmp, path)


def parse_color(text: str) -> tuple[int, int, int]:
    text = text.lstrip("#")
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except (ValueError, IndexError):
        return 0xFF, 0xFF, 0xFF


def format_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)
