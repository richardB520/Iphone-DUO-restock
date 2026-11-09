"""Polling loop: check Apple, diff against the last known state, push alerts."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from dataclasses import asdict
from typing import Callable

from . import apple, catalog, notifiers
from .config import Config, Product, format_active_hours, save_monitor_settings, save_watch

log = logging.getLogger(__name__)

Checker = Callable[..., apple.CheckResult]
Sender = Callable[[dict, notifiers.Message], dict]

# After Apple blocks us, wait this long before probing again (then 10, 20, 30 min).
# Carrying on at the normal pace only pushes the block further out.
BLOCK_COOLDOWNS = [300, 600, 1200, 1800]

HISTORY_SHOWN = 100


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
        # Set when the dashboard changes settings, so a wait ends right away.
        self.wake = threading.Event()
        self.next_due: float | None = None
        self.last_attempt: float | None = None
        self.sleeping = False
        self.paused = False
        # Times of our requests in the last hour, kept across restarts so a
        # restart cannot spend the hourly budget twice.
        self.requests: deque[float] = deque(
            t for t in self.state.get("_requests", []) if isinstance(t, (int, float))
        )
        self.trouble_since: float | None = None
        self.alerted = False
        self.history_file = cfg.state_file.with_name("history.jsonl")
        self.status: dict = {
            "region": cfg.region,
            "location": cfg.store or cfg.location,
            "products": [{"part": p.part, "name": p.name} for p in cfg.products],
            "last_check": None,
            "next_check": None,
            "last_error": None,
            "sleeping_until": None,
            "paused": False,
            "budget_until": None,
            "budget": {"used": 0, "limit": cfg.max_per_hour},
            "active_hours": _format_hours(cfg.active_hours),
            "settings": _settings(cfg),
            "checks": 0,
            "pickup": [],
            "events": [],
        }
        self.events: deque[dict] = deque(self._load_history(), maxlen=HISTORY_SHOWN)
        self.status["events"] = list(self.events)

    # -- persistence -----------------------------------------------------

    def _load_state(self) -> dict[str, dict]:
        try:
            return json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _load_history(self) -> list[dict]:
        """Most recent restock events, newest first."""
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events = []
        for line in reversed(lines[-HISTORY_SHOWN:]):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def _append_history(self, events: list[dict]) -> None:
        try:
            with self.history_file.open("a", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("could not write %s: %s", self.history_file, e)

    def _save_state(self) -> None:
        self.state["_requests"] = list(self.requests)
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
        if events:
            self._append_history(events)

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

        # Sell-outs are always recorded (they show how long stock lasts) but
        # only pushed when asked for.
        to_push = [e for e in events if e["kind"] != "sold_out" or self.cfg.notify_sold_out]
        if to_push:
            self._notify(to_push)
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
            elif not a.available and was_available:
                kind = "sold_out"
            if kind:
                if kind != "sold_out":
                    entry["last_notified"] = now
                events.append(
                    {
                        "time": now,
                        # For a sell-out: how long the stock lasted.
                        "lasted": round(now - prev["since"]) if kind == "sold_out" and "since" in prev else None,
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

    def seconds_until_active(self, at: float | None = None) -> float:
        """0 inside the daily window (or with no window), else seconds until it next opens."""
        if not self.cfg.active_hours:
            return 0.0
        start, end = self.cfg.active_hours
        now = datetime.fromtimestamp(self.clock() if at is None else at)
        minute = now.hour * 60 + now.minute
        inside = start <= minute < end if start < end else minute >= start or minute < end
        if inside:
            return 0.0
        opens = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=start)
        if opens <= now:
            opens += timedelta(days=1)
        return (opens - now).total_seconds()

    def next_delay(self) -> float:
        if self.blocks:
            cooldown = BLOCK_COOLDOWNS[min(self.blocks, len(BLOCK_COOLDOWNS)) - 1]
            return min(cooldown, self.cfg.max_backoff)
        if self.failures:
            return min(self.cfg.interval * (2 ** self.failures), self.cfg.max_backoff)
        return self.cfg.interval + random.uniform(0, self.cfg.jitter)

    def run(self, stop: threading.Event | None = None) -> None:
        self.stop_event = stop = stop or threading.Event()
        log.info(
            "monitoring %d product(s) in region %s near %s every ~%ds",
            len(self.cfg.products),
            self.cfg.region,
            self.cfg.store or self.cfg.location,
            self.cfg.interval,
        )
        while not stop.is_set():
            wait = self.step()
            # Waits are capped at a minute: the timer can stall while a Mac sleeps,
            # and a settings change from the dashboard sets `wake` to end them early.
            if self.wake.wait(wait):
                self.wake.clear()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()

    def step(self) -> float:
        """Do whatever is due now; return how long to wait before calling again."""
        now = self.clock()
        idle = self.seconds_until_active(now)
        if idle:
            # Rounded: float error otherwise shows 05:00 as 04:59:59.
            resumes = round(now + idle)
            if not self.sleeping:
                log.info("outside active hours - sleeping until %s", datetime.fromtimestamp(resumes).strftime("%H:%M"))
                self.sleeping = True
            with self.lock:
                self.status["sleeping_until"] = resumes
                self.status["next_check"] = resumes
            # A block from yesterday says nothing about today.
            self.failures = self.blocks = 0
            self.trouble_since = None
            return min(idle, 60)

        if self.sleeping:
            log.info("active hours started")
            self.sleeping = False
        with self.lock:
            self.status["sleeping_until"] = None

        if self.paused:
            return 60

        if self.next_due and now < self.next_due:
            return min(self.next_due - now, 60)

        wait = self.budget_wait(now)
        with self.lock:
            self.status["budget_until"] = round(now + wait) if wait else None
            if wait:
                self.status["next_check"] = round(now + wait)
        if wait:
            return min(wait, 60)

        self.last_attempt = now
        self.requests.append(now)
        self._check()
        self._save_state()
        with self.lock:
            self.status["budget"] = {"used": len(self.requests), "limit": self.cfg.max_per_hour}
        delay = self.next_delay()
        after = self.clock()
        self.next_due = after + delay
        # If the hourly budget is now spent, say so straight away rather than
        # showing a next check that will not happen.
        held = self.budget_wait(after)
        with self.lock:
            self.status["budget_until"] = round(after + held) if held > delay else None
            self.status["next_check"] = max(self.next_due, after + held)
        return min(delay, 60)

    def budget_wait(self, now: float) -> float:
        """0 if another request fits in this rolling hour, else seconds until one does."""
        while self.requests and self.requests[0] <= now - 3600:
            self.requests.popleft()
        with self.lock:
            self.status["budget"] = {"used": len(self.requests), "limit": self.cfg.max_per_hour}
        if len(self.requests) < self.cfg.max_per_hour:
            return 0.0
        return self.requests[-self.cfg.max_per_hour] + 3600 - now

    def _push(self, title: str, body: str) -> None:
        log.info("%s - %s", title, body)
        if self.cfg.notify:
            self.sender(self.cfg.notify, notifiers.Message(title=title, body=body))

    def _check(self) -> None:
        try:
            self.poll_once()
            self.failures = 0
            self.blocks = 0
            self.trouble_since = None
            if self.alerted:
                self.alerted = False
                self._push("✅ iPhone Duo 监控已恢复", "查询恢复正常，继续监控。")
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
            now = self.clock()
            if self.trouble_since is None:
                self.trouble_since = now
            down = (now - self.trouble_since) / 60
            if self.cfg.alert_after_minutes > 0 and not self.alerted and down >= self.cfg.alert_after_minutes:
                self.alerted = True
                self._push(
                    "⚠️ iPhone Duo 监控中断",
                    f"已经 {down:.0f} 分钟查不到库存：{e}。程序会继续自动重试，恢复后再通知你。",
                )

    def update_settings(self, interval: float, active_hours: tuple[int, int] | None) -> dict:
        """Apply new settings from the dashboard right away and save them to config.toml.

        Returns whether monitoring is active now, and if not, when it resumes.
        """
        if self.cfg.source:
            save_monitor_settings(self.cfg.source, interval, active_hours)
        with self.lock:
            self.cfg.interval = interval
            self.cfg.active_hours = active_hours
            # Re-time the next check from the last one, but never cut a block cooldown short.
            if not self.blocks and not self.failures and self.last_attempt is not None:
                self.next_due = self.last_attempt + interval
            self.status["active_hours"] = _format_hours(active_hours)
            self.status["settings"] = _settings(self.cfg)

        now = self.clock()
        idle = self.seconds_until_active(now)
        with self.lock:
            if idle:
                self.status["sleeping_until"] = self.status["next_check"] = round(now + idle)
            else:
                self.status["sleeping_until"] = None
                self.status["next_check"] = max(self.next_due or now, now)
        self.wake.set()
        log.info("settings changed: every %ds, active hours %s", interval, _format_hours(active_hours) or "all day")
        return {"active_now": not idle, "resumes_at": round(now + idle) if idle else None}

    # -- dashboard actions ------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        with self.lock:
            self.status["paused"] = paused
        log.info("monitoring %s", "paused" if paused else "resumed")
        self.wake.set()

    def check_now(self) -> dict:
        """Bring the next check forward, unless that would make things worse."""
        now = self.clock()
        if self.paused:
            return {"ok": False, "reason": "监控已暂停，先点「继续监控」"}
        if self.seconds_until_active(now):
            return {"ok": False, "reason": "现在不在监控时段内，先调整监控时段"}
        if self.blocks and self.next_due and now < self.next_due:
            return {"ok": False, "reason": "正在被 Apple 拦截的冷却中，提前查询只会被拦得更久"}
        if self.budget_wait(now):
            return {"ok": False, "reason": "这一小时的查询额度用完了，再查会被 Apple 拦截"}
        self.next_due = now
        self.wake.set()
        return {"ok": True}

    def test_notify(self) -> dict:
        if not self.cfg.notify:
            return {}
        msg = notifiers.Message(
            title="🍏 iPhone Duo 补货监控测试",
            body="如果你看到这条消息，说明通知配置正确。",
            url=apple.buy_page(self.cfg.region),
        )
        return self.sender(self.cfg.notify, msg) or {}

    def update_watch(self, location: str, products: list[Product], bark_url: str | None) -> None:
        """Change what is watched (and optionally the Bark address) and save it."""
        if self.cfg.source:
            save_watch(self.cfg.source, location, products, bark_url)
        with self.lock:
            self.cfg.location = location
            self.cfg.products = products
            if bark_url is not None:
                self.cfg.notify = {**self.cfg.notify, "bark": {**self.cfg.notify.get("bark", {}), "url": bark_url}}
            self.status["location"] = self.cfg.store or location
            self.status["products"] = [{"part": p.part, "name": p.name} for p in products]
        log.info("now watching %s near %s", ", ".join(p.name for p in products), location)
        self.check_now()

    def watch_info(self) -> dict:
        selected = {p.part for p in self.cfg.products}
        options = [
            {
                "capacity": capacity,
                "color": color,
                "label": catalog.display_name(capacity, color).removeprefix("iPhone Duo "),
                "selected": part in selected,
            }
            for (capacity, color), part in catalog.DUO_PARTS.get(self.cfg.region, {}).items()
        ]
        bark = self.cfg.notify.get("bark", {}).get("url", "")
        return {
            "location": self.cfg.location,
            "store": self.cfg.store,
            "options": options,
            "custom": [{"part": p.part, "name": p.name} for p in self.cfg.products if not p.capacity],
            # Never send the Bark key itself to the page.
            "bark": f"已设置（…{bark[-4:]}）" if bark else "",
            "channels": sorted(self.cfg.notify),
        }

    def snapshot(self) -> dict:
        with self.lock:
            status = json.loads(json.dumps(self.status))
        status["watch"] = self.watch_info()
        return status


def _format_hours(hours: tuple[int, int] | None) -> str | None:
    if not hours:
        return None
    return "–".join(f"{m // 60:02d}:{m % 60:02d}" for m in hours)


def _settings(cfg: Config) -> dict:
    return {"interval_seconds": int(cfg.interval), "active_hours": format_active_hours(cfg.active_hours)}
