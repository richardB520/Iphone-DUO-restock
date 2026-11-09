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
    # Set when the product was chosen by capacity + color rather than a raw part number.
    capacity: str = ""
    color: str = ""


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
    # Self-imposed cap on requests in any rolling hour; Apple blocks at about 30.
    max_per_hour: int = 25
    # Push a warning once checks have been failing for this long (0 = never).
    alert_after_minutes: float = 45.0
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

    products = parse_products(data.get("products", []), region)

    interval = check_interval(monitor.get("interval_seconds", 180))
    max_per_hour = int(monitor.get("max_requests_per_hour", 25))
    if max_per_hour < 1:
        raise ConfigError("[monitor] max_requests_per_hour must be at least 1")

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
        max_per_hour=max_per_hour,
        alert_after_minutes=float(monitor.get("alert_after_minutes", 45)),
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


def parse_products(raw_products, region: str) -> list[Product]:
    products = []
    if not isinstance(raw_products, list):
        raise ConfigError("products must be a list")
    for i, raw in enumerate(raw_products):
        if not isinstance(raw, dict):
            raise ConfigError(f"[[products]] entry #{i + 1} is not a table")
        part = str(raw.get("part", "")).strip()
        name = str(raw.get("name", "")).strip()
        capacity = color = ""
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
            capacity, color = catalog.normalize(capacity, color)
        if any(p.part == part for p in products):
            continue
        products.append(
            Product(part=part, name=name or part, url=str(raw.get("url", "")), capacity=capacity, color=color)
        )
    if not products:
        raise ConfigError("add at least one [[products]] entry (capacity + color, or part)")
    return products


# -- saving dashboard changes back to config.toml ---------------------------
#
# These edit the file as text so the user's comments and layout survive.


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_header(line: str) -> bool:
    return line.lstrip().startswith("[")


def _section(lines: list[str], header: str) -> tuple[int, int] | None:
    """(index of the header line, index one past the section's last line)."""
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _is_header(lines[i])), len(lines))
    return start, end


def _set_key(lines: list[str], header: str, key: str, value: str) -> None:
    """Set `key = value` inside the section `header`, creating the section if needed."""
    found = _section(lines, header)
    if found is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["\n", f"{header}\n", f"{key} = {value}\n"]
        return
    start, end = found
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    index = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
    if index is None:
        lines.insert(start + 1, f"{key} = {value}\n")
    else:
        lines[index] = f"{key} = {value}\n"


def _write(path: Path, lines: list[str]) -> None:
    text = "".join(lines)
    tomllib.loads(text)  # never save a file we could not load again
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_watch(path: Path, location: str, products: list[Product], bark_url: str | None) -> None:
    """Rewrite the location, the [[products]] entries and (if given) the Bark address."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    _set_key(lines, "[apple]", "location", _toml_str(location))

    blocks = []
    for p in products:
        if p.capacity:
            block = [f"capacity = {_toml_str(p.capacity)}\n", f"color = {_toml_str(p.color)}\n"]
        else:
            block = [f"part = {_toml_str(p.part)}\n", f"name = {_toml_str(p.name)}\n"]
        if p.url:
            block.append(f"url = {_toml_str(p.url)}\n")
        blocks += ["[[products]]\n", *block, "\n"]

    insert_at = None
    while (found := _section(lines, "[[products]]")) is not None:
        start, end = found
        # Comments and blank lines right above the next header describe that
        # header, not this entry, so they stay.
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        del lines[start:end]
        if insert_at is None:
            insert_at = start
    if insert_at is None:
        # No entries yet: put them before the first section after [apple].
        apple = _section(lines, "[apple]")
        insert_at = apple[1] if apple else len(lines)
    while insert_at < len(lines) and not lines[insert_at].strip():
        del lines[insert_at]  # collapse the blank lines the removed entries left behind
    lines[insert_at:insert_at] = blocks

    if bark_url is not None:
        _set_key(lines, "[notify.bark]", "url", _toml_str(bark_url))
        found = _section(lines, "[notify.bark]")
        pattern = re.compile(r"^\s*enabled\s*=")
        for i in range(found[0] + 1, found[1]):
            if pattern.match(lines[i]):
                lines[i] = "enabled = true\n"
    _write(path, lines)
