"""Apple Store pickup / delivery availability client.

Talks to the same `fulfillment-messages` endpoint that the "Check
availability" button on apple.com product pages uses, and turns the JSON it
returns into flat `Availability` records.
"""

from __future__ import annotations

import gzip
import html
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# region -> (site root, locale path prefix)
REGIONS: dict[str, tuple[str, str]] = {
    "us": ("https://www.apple.com", ""),
    "cn": ("https://www.apple.com.cn", ""),
    "hk": ("https://www.apple.com", "/hk"),
    "hk-zh": ("https://www.apple.com", "/hk-zh"),
    "tw": ("https://www.apple.com", "/tw"),
    "jp": ("https://www.apple.com", "/jp"),
    "sg": ("https://www.apple.com", "/sg"),
    "ca": ("https://www.apple.com", "/ca"),
    "uk": ("https://www.apple.com", "/uk"),
    "au": ("https://www.apple.com", "/au"),
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)


class AppleError(Exception):
    """Raised when Apple's endpoint fails or answers with something unusable."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def throttled(self) -> bool:
        # Apple answers 541 / 403 / 429 when it decides we are polling too fast.
        return self.status in (403, 429, 503, 541)


@dataclass
class Availability:
    part: str
    product: str
    store_number: str
    store_name: str
    city: str
    distance: str
    available: bool
    quote: str

    @property
    def key(self) -> str:
        return f"{self.part}@{self.store_number}"


@dataclass
class DeliveryQuote:
    part: str
    buyable: bool
    quote: str


@dataclass
class CheckResult:
    pickup: list[Availability] = field(default_factory=list)
    delivery: list[DeliveryQuote] = field(default_factory=list)


def site_base(region: str) -> str:
    try:
        root, prefix = REGIONS[region]
    except KeyError:
        raise ValueError(
            f"unknown region {region!r}; choose one of {', '.join(REGIONS)}"
        ) from None
    return root + prefix


def fulfillment_url(
    region: str,
    parts: list[str],
    location: str = "",
    store: str = "",
    search_nearby: bool = True,
) -> str:
    if not parts:
        raise ValueError("at least one part number is required")
    if not location and not store:
        raise ValueError("either a location (zip / city) or a store number is required")

    params: list[tuple[str, str]] = [
        ("fae", "true"),
        ("pl", "true"),
        ("mts.0", "regular"),
        ("mts.1", "compact"),
    ]
    for i, part in enumerate(parts):
        params.append((f"parts.{i}", part))
    if store:
        params.append(("store", store))
        if search_nearby:
            params.append(("searchNearby", "true"))
    else:
        params.append(("location", location))
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{site_base(region)}/shop/fulfillment-messages?{query}"


def fetch_json(url: str, referer: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise AppleError(f"HTTP {e.code} from Apple", status=e.code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AppleError(f"network error: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise AppleError("Apple returned a non-JSON response (probably a block page)") from e


def parse(payload: dict, parts: list[str]) -> CheckResult:
    """Turn a fulfillment-messages payload into flat availability records."""
    head_status = str(payload.get("head", {}).get("status", "200"))
    if head_status != "200":
        raise AppleError(f"Apple reported status {head_status}", status=int(head_status))

    content = payload.get("body", {}).get("content", {})
    result = CheckResult()

    stores = content.get("pickupMessage", {}).get("stores", []) or []
    for store in stores:
        availability = store.get("partsAvailability", {}) or {}
        for part in parts:
            info = availability.get(part)
            if not info:
                continue
            title = (
                info.get("messageTypes", {})
                .get("regular", {})
                .get("storePickupProductTitle", "")
            )
            result.pickup.append(
                Availability(
                    part=part,
                    product=title,
                    store_number=store.get("storeNumber", ""),
                    store_name=store.get("storeName", ""),
                    city=store.get("city", ""),
                    distance=store.get("storeDistanceWithUnit", ""),
                    available=info.get("pickupDisplay") == "available",
                    quote=_strip_tags(info.get("pickupSearchQuote", "")),
                )
            )

    delivery = content.get("deliveryMessage", {}) or {}
    for part in parts:
        info = delivery.get(part)
        if not isinstance(info, dict):
            continue
        regular = info.get("regular", info.get("compact", {})) or {}
        options = regular.get("deliveryOptionMessages") or regular.get("deliveryOptions") or []
        quote = ""
        if options:
            first = options[0]
            quote = first.get("displayName") or first.get("date") or ""
        result.delivery.append(
            DeliveryQuote(
                part=part,
                buyable=bool(regular.get("isBuyable", False)),
                quote=_strip_tags(quote),
            )
        )
    return result


def check(
    region: str,
    parts: list[str],
    location: str = "",
    store: str = "",
    search_nearby: bool = True,
    referer: str = "",
) -> CheckResult:
    url = fulfillment_url(region, parts, location, store, search_nearby)
    payload = fetch_json(url, referer or f"{site_base(region)}/shop/buy-iphone")
    return parse(payload, parts)


def _strip_tags(text: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return html.unescape("".join(out)).strip()
