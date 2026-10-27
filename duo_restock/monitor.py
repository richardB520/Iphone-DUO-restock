"""Polling loop: check Apple, diff against the last known state, push alerts."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from dataclasses import asdict
from typing import Callable

from . import apple, notifiers
from .config import Config

log = logging.getLogger(__name__)

Checker = Callable[..., apple.CheckResult]
Sender = Callable[[dict, notifiers.Message], dict]

# After Apple blocks us, wait this long before probing again (then 10, 20, 30 min).
# Carrying on at the normal pace only pushes the block further out.
BLOCK_COOLDOWNS = [300, 600, 1200, 1800]


class Monitor:
    def __init__(
        self,
        cfg: Config,
        checker: Checker = apple.check,
        sender: Sender = notifiers.send_all,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.checker = checker
        self.sender = sender
        self.clock = clock
        self.session = apple.Session(cfg.region)
        self.lock = threading.Lock()
        self.state: dict[str, dict] = self._load_state()
        self.failures = 0
        self.blocks = 0
        self.status: dict = {
            "region": cfg.region,
            "location": cfg.store or cfg.location,
            "products": [{"part": p.part, "name": p.name} for p in cfg.products],
            "last_check": None,
            "next_check": None,
            "last_error": None,
            "checks": 0,
            "pickup": [],
            "events": [],
        }
        self.events: deque[dict] = deque(maxlen=100)

    # -- persistence -----------------------------------------------------

    def _load_state(self) -> dict[str, dict]:
        try:
            return json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.cfg.state_file)

    # -- one round -------------------------------------------------------

    def poll_once(self) -> list[dict]:
        """Run one availability check. Returns the events it produced."""
        now = self.clock()
        # One request covers every watched model, which keeps us well under
        # Apple's per-IP request budget.
        result = self.checker(
            self.session,
            parts=self.cfg.parts,
            location=self.cfg.location,
            store=self.cfg.store,
        )
        pickup = [
            a for a in result.pickup if not self.cfg.store_filter or a.store_number in self.cfg.store_filter
        ]

        events = self._diff_pickup(pickup, now)
        self._save_state()

        with self.lock:
            self.status["last_check"] = now
            self.status["last_error"] = None
            self.status["checks"] += 1
            self.status["pickup"] = [
                {**asdict(a), "available": a.available, "name": self.cfg.product_name(a.part)}
                for a in pickup
            ]
            self.events.extendleft(events)
            self.status["events"] = list(self.events)

        if events:
            self._notify(events)
        return events

    def _diff_pickup(self, pickup: list[apple.Availability], now: float) -> list[dict]:
        events = []
        for a in pickup:
            prev = self.state.get(a.key, {})
            was_available = prev.get("available")
            entry = {
                "available": a.available,
                "since": prev.get("since", now) if was_available == a.available else now,
                "last_notified": prev.get("last_notified", 0),
            }
            kind = None
            if a.available and not was_available:
                kind = "in_stock"
            elif a.available and self.cfg.remind_minutes > 0:
                if now - entry["last_notified"] >= self.cfg.remind_minutes * 60:
                    kind = "still_in_stock"
            elif not a.available and was_available and self.cfg.notify_sold_out:
                kind = "sold_out"
            if kind:
                entry["last_notified"] = now
                events.append(
                    {
                        "time": now,
                        "kind": kind,
                        "part": a.part,
                        "name": self.cfg.product_name(a.part),
                        "store": a.store_name,
                        "store_number": a.store_number,
                        "city": a.city,
                        "quote": a.quote,
                    }
                )
            self.state[a.key] = entry
        return events

    def _notify(self, events: list[dict]) -> None:
        in_stock = [e for e in events if e["kind"] in ("in_stock", "still_in_stock")]
        title = f"🍏 iPhone Duo 有货了！({len(in_stock)} 处)" if in_stock else "iPhone Duo 已售罄"

        lines = []
        for e in events:
            icon = "❌" if e["kind"] == "sold_out" else "✅"
            where = f"{e['store']}（{e['city']}）" if e["city"] else e["store"]
            lines.append(f"{icon} {e['name']} @ {where} {e['quote']}".rstrip())

        first = events[0]["part"]
        url = self.cfg.product_url(first) or apple.buy_page(self.cfg.region)
        msg = notifiers.Message(title=title, body="\n".join(lines), url=url)
        log.info("%s\n%s", title, msg.body)
        if self.cfg.notify:
            self.sender(self.cfg.notify, msg)

    # -- loop ------------------------------------------------------------

    def next_delay(self) -> float:
        if self.blocks:
            cooldown = BLOCK_COOLDOWNS[min(self.blocks, len(BLOCK_COOLDOWNS)) - 1]
            return min(cooldown, self.cfg.max_backoff)
        if self.failures:
            return min(self.cfg.interval * (2 ** self.failures), self.cfg.max_backoff)
        return self.cfg.interval + random.uniform(0, self.cfg.jitter)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info(
            "monitoring %d product(s) in region %s near %s every ~%ds",
            len(self.cfg.products),
            self.cfg.region,
            self.cfg.store or self.cfg.location,
            self.cfg.interval,
        )
        while not stop.is_set():
            try:
                self.poll_once()
                self.failures = 0
                self.blocks = 0
                available = sum(1 for p in self.status["pickup"] if p["available"])
                log.info("check #%d ok - %d store/model combination(s) available", self.status["checks"], available)
            except apple.AppleError as e:
                if e.throttled:
                    self.blocks += 1
                    hint = (
                        f"（Apple 拦截了请求，{self.next_delay() / 60:.0f} 分钟后再试；"
                        "同一网络下的其他监控程序或频繁刷新官网也会占用额度）"
                    )
                else:
                    self.failures += 1
                    hint = ""
                log.warning("check failed: %s%s", e, hint)
                with self.lock:
                    self.status["last_error"] = str(e) + hint
            delay = self.next_delay()
            with self.lock:
                self.status["next_check"] = self.clock() + delay
            stop.wait(delay)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.status))
