"""Load and validate the TOML configuration file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .apple import REGIONS


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
    search_nearby: bool
    store_filter: list[str]
    products: list[Product]
    interval: float = 60.0
    jitter: float = 15.0
    max_backoff: float = 900.0
    notify_sold_out: bool = False
    remind_minutes: float = 0.0
    state_file: Path = Path("state.json")
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    notify: dict[str, dict] = field(default_factory=dict)

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
    return from_dict(data, base_dir=path.parent)


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
        if not part:
            raise ConfigError(f"[[products]] entry #{i + 1} is missing 'part'")
        products.append(Product(part=part, name=str(raw.get("name", part)), url=str(raw.get("url", ""))))
    if not products:
        raise ConfigError("add at least one [[products]] entry with a part number")

    interval = float(monitor.get("interval_seconds", 60))
    if interval < 20:
        raise ConfigError("[monitor] interval_seconds below 20 will get you blocked by Apple")

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
        search_nearby=bool(apple.get("search_nearby", True)),
        store_filter=[str(s) for s in apple.get("store_filter", [])],
        products=products,
        interval=interval,
        jitter=float(monitor.get("jitter_seconds", 15)),
        max_backoff=float(monitor.get("max_backoff_seconds", 900)),
        notify_sold_out=bool(monitor.get("notify_sold_out", False)),
        remind_minutes=float(monitor.get("remind_minutes", 0)),
        state_file=state_file,
        dashboard_host=str(dashboard.get("host", "127.0.0.1")),
        dashboard_port=int(dashboard.get("port", 8765)),
        notify=notify,
    )
