"""Apple Store in-store pickup availability client.

Uses `/shop/retail/pickup-message`, the endpoint the Apple Store buy pages call
for "Check availability". The older `/shop/fulfillment-messages` now answers
HTTP 541 to anything that is not a real browser carrying Apple's bot-check
tokens, so it is no longer used.

Apple's edge also rejects pickup queries that arrive without a store session,
so a `Session` first loads the bag page once to collect session cookies and
reuses them, the way a browser tab would.
"""

from __future__ import annotations

import gzip
import html
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# region -> site prefix (no trailing slash)
REGIONS: dict[str, str] = {
    "us": "https://www.apple.com",
    "cn": "https://www.apple.com.cn",
    "hk": "https://www.apple.com/hk",
    "hk-zh": "https://www.apple.com/hk-zh",
    "tw": "https://www.apple.com/tw",
    "jp": "https://www.apple.com/jp",
    "sg": "https://www.apple.com/sg",
    "ca": "https://www.apple.com/ca",
    "uk": "https://www.apple.com/uk",
    "au": "https://www.apple.com/au",
}

# A coherent desktop Chrome header set. Mixing a Chrome UA with script-style
# headers such as X-Requested-With is an easy bot signal, so none are sent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
CLIENT_HINTS = {
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

# Statuses Apple's edge uses for "you look like a bot / slow down".
THROTTLE_STATUSES = (403, 429, 503, 541)


class AppleError(Exception):
    """Raised when Apple's endpoint fails or answers with something unusable."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def throttled(self) -> bool:
        return self.status in THROTTLE_STATUSES


@dataclass
class Availability:
    part: str
    product: str
    store_number: str
    store_name: str
    city: str
    distance: str
    # "available", "unavailable", or "not_open" (Apple has not opened pickup for this model yet)
    status: str
    quote: str

    @property
    def available(self) -> bool:
        return self.status == "available"

    @property
    def key(self) -> str:
        return f"{self.part}@{self.store_number}"


@dataclass
class CheckResult:
    pickup: list[Availability] = field(default_factory=list)


def site_base(region: str) -> str:
    try:
        return REGIONS[region]
    except KeyError:
        raise ValueError(f"unknown region {region!r}; choose one of {', '.join(REGIONS)}") from None


def buy_page(region: str) -> str:
    return f"{site_base(region)}/shop/buy-iphone/iphone-duo"


def pickup_url(region: str, parts: list[str], location: str = "", store: str = "") -> str:
    if not parts:
        raise ValueError("at least one part number is required")
    if not location and not store:
        raise ValueError("either a location (zip / city) or a store number is required")

    params: list[tuple[str, str]] = [("pl", "true"), ("mts.0", "regular")]
    # A store number asks about that one store; a location returns the nearby stores.
    params.append(("store", store) if store else ("location", location))
    for i, part in enumerate(parts):
        params.append((f"parts.{i}", part))
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{site_base(region)}/shop/retail/pickup-message?{query}"


class Session:
    """One browser-like session per region: a cookie jar plus a warmed-up flag."""

    def __init__(self, region: str, timeout: float = 15.0):
        self.region = region
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.warmed = False

    def warm(self) -> None:
        """Load the bag page, which hands out the store session cookies."""
        headers = {
            **CLIENT_HINTS,
            "upgrade-insecure-requests": "1",
            "user-agent": USER_AGENT,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,image/apng,*/*;q=0.8",
            "sec-fetch-site": "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-user": "?1",
            "sec-fetch-dest": "document",
            "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }
        try:
            self._get(f"{site_base(self.region)}/shop/bag", headers)
        except AppleError:
            # A failed warm-up is not fatal; the query itself reports what went wrong.
            return
        self.warmed = len(self.jar) > 0

    def forget(self) -> None:
        """After a block the session may be burned; warm up again next time."""
        self.jar.clear()
        self.warmed = False

    def fetch_json(self, url: str) -> dict:
        if not self.warmed:
            self.warm()
        headers = {
            **CLIENT_HINTS,
            "user-agent": USER_AGENT,
            "accept": "*/*",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": buy_page(self.region),
            "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }
        try:
            raw = self._get(url, headers)
        except AppleError as e:
            if e.throttled:
                self.forget()
            raise
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # A 200 with HTML is Apple's block page in disguise.
            self.forget()
            raise AppleError("Apple returned a web page instead of data (blocked)", status=541) from None

    def _get(self, url: str, headers: dict[str, str]) -> bytes:
        req = urllib.request.Request(url, headers={**headers, "accept-encoding": "gzip"})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            raise AppleError(f"HTTP {e.code} from Apple", status=e.code) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise AppleError(f"network error: {e}") from e


def parse(payload: dict, parts: list[str]) -> CheckResult:
    """Turn a pickup-message payload into flat availability records."""
    head_status = str(payload.get("head", {}).get("status", "200"))
    if head_status != "200":
        status = int(head_status) if head_status.isdigit() else None
        raise AppleError(f"Apple reported status {head_status}", status=status)

    body = payload.get("body", {}) or {}
    if body.get("errorMessage"):
        raise AppleError(f"Apple error: {_clean(body['errorMessage'])}")

    # Current responses put stores at body.stores; older ones nested them under
    # body.content.pickupMessage.stores. Accept both.
    stores = body.get("stores") or body.get("content", {}).get("pickupMessage", {}).get("stores") or []
    if not stores:
        raise AppleError(
            "Apple returned no stores - check the part numbers match the region, "
            "and that the location is valid"
        )

    result = CheckResult()
    for store in stores:
        availability = store.get("partsAvailability", {}) or {}
        for part in parts:
            info = availability.get(part)
            if not info:
                continue
            title = info.get("messageTypes", {}).get("regular", {}).get("storePickupProductTitle", "")
            result.pickup.append(
                Availability(
                    part=part,
                    product=title,
                    store_number=store.get("storeNumber", ""),
                    store_name=store.get("storeName", ""),
                    city=store.get("city", ""),
                    distance=store.get("storeDistanceWithUnit", ""),
                    status=_status(info.get("pickupDisplay", "")),
                    quote=_clean(info.get("pickupSearchQuote", "")),
                )
            )
    return result


def check(
    session: Session,
    parts: list[str],
    location: str = "",
    store: str = "",
) -> CheckResult:
    url = pickup_url(session.region, parts, location, store)
    return parse(session.fetch_json(url), parts)


def _status(pickup_display: str) -> str:
    if pickup_display == "available":
        return "available"
    # Before Apple opens in-store pickup for a model every store reports "default".
    if pickup_display == "default":
        return "not_open"
    return "unavailable"


def _clean(text: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return html.unescape("".join(out)).strip()
