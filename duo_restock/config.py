"""Load and validate the TOML configuration file."""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog
from .apple import REGIONS


log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass
class Product:
    part: str
    name: str
    url: str = ""


@dataclass
class Config:
    region: str
    location: str
    store: str
    store_filter: list[str]
    products: list[Product]
    interval: float = 180.0
    jitter: float = 15.0
    max_backoff: float = 1800.0
    notify_sold_out: bool = False
    remind_minutes: float = 0.0
    # Daily monitoring window in local time as (start, end) minutes after midnight;
    # None means around the clock.
    active_hours: tuple[int, int] | None = None
    state_file: Path = Path("state.json")
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    notify: dict[str, dict] = field(default_factory=dict)
    # The file this config was loaded from, so dashboard changes can be saved back.
    source: Path | None = None

    @property
    def parts(self) -> list[str]:
        return [p.part for p in self.products]

    def product_name(self, part: str) -> str:
        for p in self.products:
            if p.part == part:
                return p.name
        return part

    def product_url(self, part: str) -> str:
        for p in self.products:
            if p.part == part and p.url:
                return p.url
        return ""


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"config file {path} not found - copy config.example.toml to {path} first"
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    cfg = from_dict(data, base_dir=path.parent)
    cfg.source = path
    return cfg


def from_dict(data: dict, base_dir: Path = Path(".")) -> Config:
    apple = data.get("apple", {})
    monitor = data.get("monitor", {})
    dashboard = data.get("dashboard", {})

    region = str(apple.get("region", "us")).lower()
    if region not in REGIONS:
        raise ConfigError(f"[apple] region must be one of: {', '.join(REGIONS)}")

    location = str(apple.get("location", "")).strip()
    store = str(apple.get("store", "")).strip()
    if not location and not store:
        raise ConfigError("[apple] set either location (zip / city) or store (e.g. R095)")

    products = []
    for i, raw in enumerate(data.get("products", [])):
        part = str(raw.get("part", "")).strip()
        name = str(raw.get("name", "")).strip()
        if not part:
            capacity, color = str(raw.get("capacity", "")), str(raw.get("color", ""))
            if not capacity or not color:
                raise ConfigError(
                    f"[[products]] entry #{i + 1} needs either 'part' or both 'capacity' and 'color'"
                )
            part = catalog.lookup(region, capacity, color) or ""
            if not part:
                raise ConfigError(
                    f"[[products]] entry #{i + 1}: no built-in part number for {capacity} {color} "
                    f"in region {region} - run `python3 -m duo_restock models` to see the choices, "
                    "or set 'part' directly"
                )
            name = name or catalog.display_name(capacity, color)
        products.append(Product(part=part, name=name or part, url=str(raw.get("url", ""))))
    if not products:
        raise ConfigError("add at least one [[products]] entry (capacity + color, or part)")

    interval = check_interval(monitor.get("interval_seconds", 180))

    active_hours = _parse_active_hours(monitor.get("active_hours"))

    state_file = Path(monitor.get("state_file", "state.json"))
    if not state_file.is_absolute():
        state_file = base_dir / state_file

    notify = {
        name: cfg
        for name, cfg in data.get("notify", {}).items()
        if isinstance(cfg, dict) and cfg.get("enabled", True)
    }

    return Config(
        region=region,
        location=location,
        store=store,
        store_filter=[str(s) for s in apple.get("store_filter", [])],
        products=products,
        interval=interval,
        jitter=float(monitor.get("jitter_seconds", 15)),
        max_backoff=float(monitor.get("max_backoff_seconds", 1800)),
        notify_sold_out=bool(monitor.get("notify_sold_out", False)),
        remind_minutes=float(monitor.get("remind_minutes", 0)),
        active_hours=active_hours,
        state_file=state_file,
        dashboard_host=str(dashboard.get("host", "127.0.0.1")),
        dashboard_port=int(dashboard.get("port", 8765)),
        notify=notify,
    )


def _parse_active_hours(raw) -> tuple[int, int] | None:
    if not raw:
        return None
    if not (isinstance(raw, list) and len(raw) == 2):
        raise ConfigError('[monitor] active_hours must look like ["05:00", "10:00"]')
    minutes = []
    for value in raw:
        try:
            hour, minute = (int(x) for x in str(value).split(":"))
        except ValueError:
            raise ConfigError(f"[monitor] active_hours: {value!r} is not a HH:MM time") from None
        if not (0 <= hour <= 24 and 0 <= minute < 60) or hour * 60 + minute > 1440:
            raise ConfigError(f"[monitor] active_hours: {value!r} is not a valid time")
        minutes.append(hour * 60 + minute)
    if minutes[0] == minutes[1]:
        raise ConfigError("[monitor] active_hours start and end must differ")
    return minutes[0], minutes[1]


def check_interval(raw) -> float:
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"[monitor] interval_seconds must be a number, got {raw!r}") from None
    if interval < 20:
        raise ConfigError("[monitor] interval_seconds below 20 will get you blocked by Apple")
    if interval < 150:
        # Measured: Apple lets roughly 30 pickup queries per IP per hour through.
        log.warning(
            "interval_seconds = %d means ~%d requests/hour; Apple blocks at about 30/hour, "
            "so expect a 15-30 minute block roughly every hour. 180 is recommended.",
            interval,
            3600 // interval,
        )
    return interval


def parse_active_hours(raw) -> tuple[int, int] | None:
    return _parse_active_hours(raw)


def format_active_hours(hours: tuple[int, int] | None) -> list[str] | None:
    if not hours:
        return None
    return [f"{m // 60:02d}:{m % 60:02d}" for m in hours]


def save_monitor_settings(path: Path, interval: float, active_hours: tuple[int, int] | None) -> None:
    """Rewrite interval_seconds and active_hours in [monitor], keeping every other line."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    values = {
        "interval_seconds": f"interval_seconds = {int(interval)}\n",
        "active_hours": (
            "active_hours = [{}]\n".format(", ".join(f'"{t}"' for t in format_active_hours(active_hours)))
            if active_hours
            else None
        ),
    }

    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "[monitor]")
    except StopIteration:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", "[monitor]\n"]
        start = len(lines) - 1
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )

    for key, new in values.items():
        pattern = re.compile(rf"^\s*{key}\s*=")
        index = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
        if index is not None:
            if new is None:
                del lines[index]
                end -= 1
            else:
                lines[index] = new
        elif new is not None:
            lines.insert(start + 1, new)
            end += 1

    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(path)
