import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from duo_restock import apple, catalog, config, notifiers
from duo_restock.monitor import Monitor

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pickup_us.json").read_text())
BLACK, WHITE = "MK1H4LL/A", "MK1G4LL/A"
PARTS = [BLACK, WHITE]


def make_config(tmp: str, **monitor) -> config.Config:
    return config.from_dict(
        {
            "apple": {"region": "us", "location": "10001"},
            "products": [
                {"capacity": "512gb", "color": "nightsky", "url": "https://example.com/buy"},
                {"part": WHITE, "name": "Duo 512 White"},
            ],
            "monitor": {"state_file": "state.json", **monitor},
            "notify": {"webhook": {"url": "https://example.com/hook"}},
        },
        base_dir=Path(tmp),
    )


def payload_with(first_available: bool) -> dict:
    data = json.loads(json.dumps(FIXTURE))
    store = data["body"]["stores"][0]
    store["partsAvailability"][BLACK]["pickupDisplay"] = "available" if first_available else "unavailable"
    return data


class FakeApple:
    def __init__(self, *payloads):
        self.payloads = list(payloads)

    def __call__(self, session, **kwargs):
        return apple.parse(self.payloads.pop(0), kwargs["parts"])


class ParseTests(unittest.TestCase):
    def test_parses_pickup_per_store_and_part(self):
        result = apple.parse(FIXTURE, PARTS)
        self.assertEqual(len(result.pickup), 4)
        first = result.pickup[0]
        self.assertEqual(first.key, "MK1H4LL/A@R095")
        self.assertTrue(first.available)
        self.assertEqual(first.quote, "Available Today")
        self.assertEqual(first.product, "iPhone Duo 512GB Night Sky")
        self.assertFalse(any(a.available for a in result.pickup[1:]))

    def test_default_display_means_pickup_not_open(self):
        soho_white = apple.parse(FIXTURE, PARTS).pickup[3]
        self.assertEqual(soho_white.status, "not_open")
        self.assertFalse(soho_white.available)

    def test_unescapes_entities(self):
        self.assertEqual(apple.parse(FIXTURE, PARTS).pickup[2].quote, "Currently unavailable – check back")

    def test_accepts_legacy_nested_layout(self):
        legacy = {"head": {"status": "200"}, "body": {"content": {"pickupMessage": {"stores": FIXTURE["body"]["stores"]}}}}
        self.assertEqual(len(apple.parse(legacy, PARTS).pickup), 4)

    def test_ignores_unrequested_parts(self):
        result = apple.parse(FIXTURE, [WHITE])
        self.assertEqual({a.part for a in result.pickup}, {WHITE})

    def test_bad_head_status_raises_throttled_error(self):
        with self.assertRaises(apple.AppleError) as ctx:
            apple.parse({"head": {"status": "541"}, "body": {}}, PARTS)
        self.assertTrue(ctx.exception.throttled)

    def test_error_message_and_empty_stores_raise(self):
        with self.assertRaisesRegex(apple.AppleError, "invalid zip"):
            apple.parse({"head": {"status": "200"}, "body": {"errorMessage": "invalid zip"}}, PARTS)
        with self.assertRaisesRegex(apple.AppleError, "no stores"):
            apple.parse({"head": {"status": "200"}, "body": {"stores": []}}, PARTS)


class UrlTests(unittest.TestCase):
    def test_location_query(self):
        url = apple.pickup_url("us", PARTS, location="10001")
        self.assertTrue(url.startswith("https://www.apple.com/shop/retail/pickup-message?"))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["parts.0"], [BLACK])
        self.assertEqual(q["parts.1"], [WHITE])
        self.assertEqual(q["location"], ["10001"])
        self.assertNotIn("store", q)

    def test_store_query_and_regions(self):
        url = apple.pickup_url("cn", ["MK2Q4CH/A"], store="R705")
        self.assertTrue(url.startswith("https://www.apple.com.cn/shop/retail/pickup-message?"))
        self.assertIn("store=R705", url)
        self.assertNotIn("location", url)
        self.assertTrue(apple.pickup_url("hk", ["X"], location="x").startswith("https://www.apple.com/hk/shop/"))

    def test_chinese_location_is_encoded(self):
        url = apple.pickup_url("cn", ["X"], location="上海 上海 黄浦区")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["location"], ["上海 上海 黄浦区"])

    def test_requires_location_or_store(self):
        with self.assertRaises(ValueError):
            apple.pickup_url("us", PARTS)


class FakeAppleServer:
    """Local stand-in for apple.com: hands out a session cookie on /shop/bag and
    only answers pickup queries that carry it."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.mode = "ok"  # ok | block | html
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, dict(self.headers)))
                if self.path == "/shop/bag":
                    self.send_response(200)
                    self.send_header("Set-Cookie", "dssid2=abc; Path=/")
                    self.end_headers()
                    self.wfile.write(b"<html>bag</html>")
                    return
                if outer.mode == "block" or "dssid2=abc" not in self.headers.get("Cookie", ""):
                    self.send_response(541)
                    self.end_headers()
                    self.wfile.write(b"<html>Page Not Found</html>")
                    return
                body = b"<html>blocked</html>" if outer.mode == "html" else json.dumps(FIXTURE).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        apple.REGIONS["test"] = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        apple.REGIONS.pop("test", None)

    def paths(self):
        return [p.split("?")[0] for p, _ in self.requests]


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAppleServer()
        self.session = apple.Session("test", timeout=5)

    def tearDown(self):
        self.fake.close()

    def test_warms_up_once_then_reuses_cookies(self):
        apple.check(self.session, PARTS, location="10001")
        result = apple.check(self.session, PARTS, location="10001")
        self.assertEqual(len(result.pickup), 4)
        self.assertEqual(
            self.fake.paths(),
            ["/shop/bag", "/shop/retail/pickup-message", "/shop/retail/pickup-message"],
        )

    def test_sends_browser_like_headers(self):
        apple.check(self.session, PARTS, location="10001")
        headers = {k.lower(): v for k, v in self.fake.requests[-1][1].items()}
        self.assertIn("Chrome/", headers["user-agent"])
        self.assertEqual(headers["sec-fetch-mode"], "cors")
        self.assertTrue(headers["referer"].endswith("/shop/buy-iphone/iphone-duo"))
        self.assertNotIn("x-requested-with", headers)

    def test_block_is_throttled_and_forces_new_warmup(self):
        apple.check(self.session, PARTS, location="10001")
        self.fake.mode = "block"
        with self.assertRaises(apple.AppleError) as ctx:
            apple.check(self.session, PARTS, location="10001")
        self.assertTrue(ctx.exception.throttled)
        self.assertFalse(self.session.warmed)
        self.fake.mode = "ok"
        apple.check(self.session, PARTS, location="10001")
        self.assertEqual(self.fake.paths().count("/shop/bag"), 2)

    def test_html_with_200_counts_as_block(self):
        self.fake.mode = "html"
        with self.assertRaises(apple.AppleError) as ctx:
            apple.check(self.session, PARTS, location="10001")
        self.assertTrue(ctx.exception.throttled)


class CatalogTests(unittest.TestCase):
    def test_every_region_has_all_eight_models(self):
        for region in apple.REGIONS:
            self.assertEqual(len(catalog.DUO_PARTS[region]), 8, region)

    def test_lookup_is_forgiving_about_spelling(self):
        self.assertEqual(catalog.lookup("us", "512GB", "Night Sky"), BLACK)
        self.assertEqual(catalog.lookup("cn", "1TB", "star-white"), "MK2T4CH/A")
        self.assertIsNone(catalog.lookup("us", "64gb", "nightsky"))


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = config.load(Path(__file__).parent.parent / "config.example.toml")
        self.assertEqual(cfg.region, "us")
        self.assertEqual(cfg.parts, [BLACK, WHITE])
        self.assertEqual(cfg.products[0].name, "iPhone Duo 512GB Night Sky")
        self.assertEqual(list(cfg.notify), ["desktop"])

    def test_capacity_and_color_resolve_per_region(self):
        cfg = config.from_dict(
            {"apple": {"region": "cn", "location": "上海 上海 黄浦区"}, "products": [{"capacity": "256gb", "color": "starwhite"}]}
        )
        self.assertEqual(cfg.parts, ["MK2M4CH/A"])

    def test_unknown_model_is_rejected(self):
        with self.assertRaisesRegex(config.ConfigError, "models"):
            config.from_dict({"apple": {"location": "1"}, "products": [{"capacity": "64gb", "color": "red"}]})
        with self.assertRaises(config.ConfigError):
            config.from_dict({"apple": {"location": "1"}, "products": [{"color": "nightsky"}]})

    def test_active_hours_validation(self):
        base = {"apple": {"location": "1"}, "products": [{"part": "X"}]}
        self.assertEqual(config.from_dict({**base, "monitor": {"active_hours": ["05:00", "10:00"]}}).active_hours, (300, 600))
        self.assertIsNone(config.from_dict(base).active_hours)
        for bad in (["5am", "10:00"], ["05:00"], ["05:00", "05:00"], ["25:00", "10:00"]):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.from_dict({**base, "monitor": {"active_hours": bad}})

    def test_rejects_too_fast_interval(self):
        with self.assertRaises(config.ConfigError):
            config.from_dict(
                {"apple": {"location": "1"}, "products": [{"part": "X"}], "monitor": {"interval_seconds": 5}}
            )

    def test_rejects_missing_products_and_region(self):
        with self.assertRaises(config.ConfigError):
            config.from_dict({"apple": {"location": "1"}})
        with self.assertRaises(config.ConfigError):
            config.from_dict({"apple": {"region": "mars", "location": "1"}, "products": [{"part": "X"}]})

    def test_disabled_channel_is_dropped(self):
        cfg = config.from_dict(
            {
                "apple": {"location": "1"},
                "products": [{"part": "X"}],
                "notify": {"bark": {"enabled": False, "url": "u"}, "ntfy": {"topic": "t"}},
            }
        )
        self.assertEqual(list(cfg.notify), ["ntfy"])


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self.now = 1000.0

    def tearDown(self):
        self.tmp.cleanup()

    def monitor(self, fake, **monitor_cfg):
        return Monitor(
            make_config(self.tmp.name, **monitor_cfg),
            checker=fake,
            sender=lambda channels, msg: self.sent.append(msg),
            clock=lambda: self.now,
        )

    def test_alerts_only_on_transition_to_available(self):
        m = self.monitor(FakeApple(payload_with(False), payload_with(True), payload_with(True), payload_with(False)))
        self.assertEqual(m.poll_once(), [])
        events = m.poll_once()
        self.assertEqual([e["kind"] for e in events], ["in_stock"])
        self.assertEqual(events[0]["store"], "Fifth Avenue")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("有货", self.sent[0].title)
        self.assertIn("iPhone Duo 512GB Night Sky", self.sent[0].body)
        self.assertEqual(self.sent[0].url, "https://example.com/buy")
        self.assertEqual(m.poll_once(), [])  # still available: no repeat
        self.now += 600
        sold = m.poll_once()  # sold out: recorded, but not pushed by default
        self.assertEqual([(e["kind"], e["lasted"]) for e in sold], [("sold_out", 600)])
        self.assertEqual(len(self.sent), 1)

    def test_first_check_alerts_when_already_in_stock(self):
        m = self.monitor(FakeApple(payload_with(True)))
        self.assertEqual([e["kind"] for e in m.poll_once()], ["in_stock"])

    def test_snapshot_carries_status_for_dashboard(self):
        m = self.monitor(FakeApple(payload_with(True)))
        m.poll_once()
        pickup = m.snapshot()["pickup"]
        self.assertEqual([p["status"] for p in pickup], ["available", "unavailable", "unavailable", "not_open"])
        self.assertTrue(pickup[0]["available"])

    def test_sold_out_and_reminders_when_enabled(self):
        m = self.monitor(
            FakeApple(payload_with(True), payload_with(True), payload_with(True), payload_with(False)),
            notify_sold_out=True,
            remind_minutes=10,
        )
        m.poll_once()
        self.now += 60
        self.assertEqual(m.poll_once(), [])
        self.now += 600
        self.assertEqual([e["kind"] for e in m.poll_once()], ["still_in_stock"])
        self.assertEqual([e["kind"] for e in m.poll_once()], ["sold_out"])

    def test_state_survives_restart(self):
        self.monitor(FakeApple(payload_with(True))).poll_once()
        self.assertEqual(len(self.sent), 1)
        restarted = self.monitor(FakeApple(payload_with(True)))
        self.assertEqual(restarted.poll_once(), [])
        self.assertEqual(len(self.sent), 1)

    def test_store_filter(self):
        m = self.monitor(FakeApple(payload_with(True)))
        m.cfg.store_filter = ["R227"]
        self.assertEqual(m.poll_once(), [])
        self.assertEqual({p["store_number"] for p in m.snapshot()["pickup"]}, {"R227"})

    def test_active_hours_window(self):
        from datetime import datetime

        m = self.monitor(FakeApple(), active_hours=["05:00", "10:00"])
        at = lambda h, mi=0: datetime(2026, 10, 27, h, mi).timestamp()
        self.now = at(5)
        self.assertEqual(m.seconds_until_active(), 0)
        self.now = at(9, 59)
        self.assertEqual(m.seconds_until_active(), 0)
        self.now = at(4, 30)
        self.assertEqual(m.seconds_until_active(), 30 * 60)
        self.now = at(10)
        self.assertEqual(m.seconds_until_active(), 19 * 3600)
        self.assertEqual(m.snapshot()["active_hours"], "05:00–10:00")

    def test_active_hours_across_midnight(self):
        from datetime import datetime

        m = self.monitor(FakeApple(), active_hours=["22:00", "02:00"])
        self.now = datetime(2026, 10, 27, 23, 0).timestamp()
        self.assertEqual(m.seconds_until_active(), 0)
        self.now = datetime(2026, 10, 27, 1, 0).timestamp()
        self.assertEqual(m.seconds_until_active(), 0)
        self.now = datetime(2026, 10, 27, 12, 0).timestamp()
        self.assertEqual(m.seconds_until_active(), 10 * 3600)

    def test_step_sleeps_outside_active_hours_without_polling(self):
        from datetime import datetime

        m = self.monitor(FakeApple(), active_hours=["05:00", "10:00"])
        self.now = datetime(2026, 10, 27, 3, 0).timestamp()
        self.assertEqual(m.step(), 60)
        self.assertEqual(m.snapshot()["checks"], 0)
        self.assertEqual(m.snapshot()["sleeping_until"], datetime(2026, 10, 27, 5, 0).timestamp())

    def test_extending_hours_resumes_immediately(self):
        from datetime import datetime

        m = self.monitor(FakeApple(payload_with(False)), active_hours=["05:00", "16:00"])
        self.now = datetime(2026, 10, 27, 16, 30).timestamp()
        m.step()
        self.assertIsNotNone(m.snapshot()["sleeping_until"])

        outcome = m.update_settings(180, (300, 1080))  # 05:00-18:00
        self.assertEqual(outcome, {"active_now": True, "resumes_at": None})
        self.assertTrue(m.wake.is_set())
        self.assertIsNone(m.snapshot()["sleeping_until"])
        m.step()
        self.assertEqual(m.snapshot()["checks"], 1)

    def test_settings_outside_window_report_when_it_resumes(self):
        from datetime import datetime

        m = self.monitor(FakeApple())
        ticks = iter([datetime(2026, 10, 27, 16, 30).timestamp(), datetime(2026, 10, 27, 16, 30, 0, 5000).timestamp()])
        m.clock = lambda: next(ticks)  # the clock moves between reads
        outcome = m.update_settings(180, (300, 360))  # 05:00-06:00
        self.assertFalse(outcome["active_now"])
        self.assertEqual(outcome["resumes_at"], datetime(2026, 10, 28, 5, 0).timestamp())
        self.assertEqual(m.snapshot()["sleeping_until"], outcome["resumes_at"])

    def test_shorter_interval_retimes_next_check(self):
        m = self.monitor(FakeApple(payload_with(False), payload_with(False)))
        m.step()
        self.now += 40
        self.assertGreater(m.step(), 0)  # still waiting on the 180 s interval
        self.assertEqual(m.snapshot()["checks"], 1)
        m.update_settings(30, None)
        m.step()
        self.assertEqual(m.snapshot()["checks"], 2)

    def test_settings_change_never_cuts_a_block_cooldown_short(self):
        m = self.monitor(FakeApple())
        m.last_attempt, m.blocks, m.next_due = self.now, 1, self.now + 300
        m.update_settings(30, None)
        self.assertEqual(m.next_due, self.now + 300)

    def test_stop_ends_run_promptly(self):
        m = self.monitor(FakeApple(payload_with(False)))
        thread = threading.Thread(target=m.run)
        thread.start()
        for _ in range(100):
            if m.snapshot()["checks"]:
                break
            threading.Event().wait(0.02)
        m.stop()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_hourly_budget_holds_requests_back(self):
        m = self.monitor(FakeApple(*[payload_with(False)] * 4), max_requests_per_hour=3, interval_seconds=30)
        start = self.now
        for _ in range(3):
            m.step()
            self.now += 50  # past the 30 s interval plus jitter
        self.assertEqual(m.snapshot()["checks"], 3)
        self.assertEqual(m.snapshot()["budget_until"], round(start + 3600))  # shown as soon as it is spent
        wait = m.step()  # a 4th request within the hour is held back
        self.assertEqual(m.snapshot()["checks"], 3)
        self.assertEqual(wait, 60)
        self.assertEqual(m.snapshot()["budget_until"], round(start + 3600))
        self.assertFalse(m.check_now()["ok"])
        self.now = start + 3601
        m.step()
        self.assertEqual(m.snapshot()["checks"], 4)
        # Full again until the second request of the first hour ages out.
        self.assertEqual(m.snapshot()["budget_until"], round(start + 50 + 3600))

    def test_budget_survives_restart(self):
        m = self.monitor(FakeApple(payload_with(False), payload_with(False)), max_requests_per_hour=2)
        m.step()
        self.now += 200
        m.step()
        restarted = self.monitor(FakeApple(), max_requests_per_hour=2)
        self.assertGreater(restarted.step(), 0)
        self.assertEqual(restarted.snapshot()["checks"], 0)
        self.assertEqual(restarted.snapshot()["budget"], {"used": 2, "limit": 2})

    def test_history_survives_restart(self):
        m = self.monitor(FakeApple(payload_with(True), payload_with(False)))
        m.poll_once()
        self.now += 120
        m.poll_once()
        events = self.monitor(FakeApple()).snapshot()["events"]
        self.assertEqual([e["kind"] for e in events], ["sold_out", "in_stock"])
        self.assertEqual(events[0]["lasted"], 120)

    def test_pause_and_check_now(self):
        m = self.monitor(FakeApple(payload_with(False), payload_with(False)))
        m.set_paused(True)
        m.step()
        self.assertEqual(m.snapshot()["checks"], 0)
        self.assertFalse(m.check_now()["ok"])
        m.set_paused(False)
        m.step()
        self.now += 10
        m.step()
        self.assertEqual(m.snapshot()["checks"], 1)  # interval not up yet
        self.assertTrue(m.check_now()["ok"])
        m.step()
        self.assertEqual(m.snapshot()["checks"], 2)

    def test_check_now_refused_during_block_cooldown(self):
        m = self.monitor(FakeApple())
        m.blocks, m.next_due = 1, self.now + 300
        self.assertFalse(m.check_now()["ok"])
        self.assertEqual(m.next_due, self.now + 300)

    def test_alerts_once_when_down_long_and_again_on_recovery(self):
        def blocked(session, **kw):
            raise apple.AppleError("HTTP 541 from Apple", status=541)

        m = self.monitor(blocked, alert_after_minutes=10)
        m.step()
        self.assertEqual(self.sent, [])
        self.now += 700
        m.step()
        self.now += 700
        m.step()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("中断", self.sent[0].title)
        m.checker = FakeApple(payload_with(False))
        self.now += 1300
        m.step()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("恢复", self.sent[1].title)

    def test_block_cooldowns_then_error_backoff(self):
        m = self.monitor(FakeApple())
        m.blocks = 1
        self.assertEqual(m.next_delay(), 300)
        m.blocks = 3
        self.assertEqual(m.next_delay(), 1200)
        m.blocks = 9
        self.assertEqual(m.next_delay(), 1800)
        m.blocks, m.failures = 0, 1
        self.assertEqual(m.next_delay(), 360)  # interval 180 doubled
        m.failures = 10
        self.assertEqual(m.next_delay(), 1800)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text((Path(__file__).parent.parent / "config.example.toml").read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_keeps_comments_and_other_settings(self):
        before = self.path.read_text()
        config.save_monitor_settings(self.path, 300, (360, 1440))
        cfg = config.load(self.path)
        self.assertEqual((cfg.interval, cfg.active_hours, cfg.source), (300, (360, 1440), self.path))
        self.assertIn('active_hours = ["06:00", "24:00"]', self.path.read_text())
        changed = [
            (a, b) for a, b in zip(before.splitlines(), self.path.read_text().splitlines()) if a != b
        ]
        self.assertEqual(len(changed), 2)
        self.assertEqual(len(before.splitlines()), len(self.path.read_text().splitlines()))

    def test_save_all_day_removes_window_and_can_add_it_back(self):
        config.save_monitor_settings(self.path, 180, None)
        self.assertIsNone(config.load(self.path).active_hours)
        config.save_monitor_settings(self.path, 180, (300, 600))
        self.assertEqual(config.load(self.path).active_hours, (300, 600))

    def test_save_creates_monitor_section_when_missing(self):
        self.path.write_text('[apple]\nlocation = "1"\n\n[[products]]\npart = "X"\n')
        config.save_monitor_settings(self.path, 240, (300, 600))
        cfg = config.load(self.path)
        self.assertEqual((cfg.interval, cfg.active_hours, cfg.parts), (240, (300, 600), ["X"]))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        from duo_restock import dashboard

        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "config.toml"
        path.write_text((Path(__file__).parent.parent / "config.example.toml").read_text())
        self.monitor = Monitor(config.load(path), checker=FakeApple(), sender=lambda c, m: None)
        self.server = dashboard.serve(self.monitor, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, body, content_type="application/json"):
        import urllib.error
        import urllib.request

        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + "/api/settings", data=data, headers={"Content-Type": content_type}, method="POST"
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_settings_change_applies_live_and_is_saved(self):
        code, status = self.post({"interval_seconds": 300, "active_hours": ["06:00", "24:00"]})
        self.assertEqual(code, 200)
        self.assertEqual(status["settings"], {"interval_seconds": 300, "active_hours": ["06:00", "24:00"]})
        self.assertIn("active_now", status["outcome"])
        self.assertEqual((self.monitor.cfg.interval, self.monitor.cfg.active_hours), (300, (360, 1440)))
        saved = config.load(self.monitor.cfg.source)
        self.assertEqual((saved.interval, saved.active_hours), (300, (360, 1440)))

    def test_all_day(self):
        code, status = self.post({"interval_seconds": 180, "active_hours": None})
        self.assertEqual(code, 200)
        self.assertIsNone(self.monitor.cfg.active_hours)
        self.assertIsNone(status["active_hours"])

    def test_rejects_bad_values_without_changing_anything(self):
        before = self.monitor.cfg.source.read_text()
        self.assertEqual(self.post({"interval_seconds": 5, "active_hours": None})[0], 400)
        self.assertEqual(self.post({"interval_seconds": 180, "active_hours": ["9am", "10:00"]})[0], 400)
        self.assertEqual(self.post({"active_hours": None})[0], 400)
        self.assertEqual(self.post(b"not json")[0], 400)
        self.assertEqual(self.monitor.cfg.source.read_text(), before)
        self.assertEqual(self.monitor.cfg.interval, 180)

    def call(self, path, body=None):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            self.base + path, data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_watch_change_applies_and_is_saved_without_leaking_bark_key(self):
        body = {
            "location": "07302",
            "products": [{"capacity": "1tb", "color": "nightsky"}, {"capacity": "1tb", "color": "starwhite"}],
            "bark_url": "https://api.day.app/SECRETKEY1234",
        }
        code, status = self.call("/api/watch", body)
        self.assertEqual(code, 200)
        self.assertEqual(self.monitor.cfg.parts, ["MK1L4LL/A", "MK1J4LL/A"])
        self.assertEqual(status["location"], "07302")
        self.assertNotIn("SECRETKEY", json.dumps(status))
        self.assertEqual(status["watch"]["bark"], "已设置（…1234）")
        self.assertEqual([o["label"] for o in status["watch"]["options"] if o["selected"]], ["1TB Night Sky", "1TB Star White"])
        saved = config.load(self.monitor.cfg.source)
        self.assertEqual((saved.location, saved.parts), ("07302", ["MK1L4LL/A", "MK1J4LL/A"]))
        self.assertEqual(saved.notify["bark"]["url"], "https://api.day.app/SECRETKEY1234")
        self.assertIn("# 想监控的型号", self.monitor.cfg.source.read_text())

    def test_watch_rejects_bad_input_without_saving(self):
        before = self.monitor.cfg.source.read_text()
        ok = [{"capacity": "1tb", "color": "nightsky"}]
        self.assertEqual(self.call("/api/watch", {"location": "07302", "products": []})[0], 400)
        self.assertEqual(self.call("/api/watch", {"location": "", "products": ok})[0], 400)
        self.assertEqual(self.call("/api/watch", {"location": "1", "products": [{"capacity": "9tb", "color": "red"}]})[0], 400)
        self.assertEqual(self.call("/api/watch", {"location": "1", "products": ok, "bark_url": "javascript:x"})[0], 400)
        self.assertEqual(self.monitor.cfg.source.read_text(), before)

    def test_pause_check_now_and_test_notify_endpoints(self):
        self.monitor.cfg.active_hours = None
        self.assertTrue(self.call("/api/pause", {"paused": True})[1]["paused"])
        self.assertFalse(self.call("/api/check-now")[1]["result"]["ok"])
        self.assertFalse(self.call("/api/pause", {"paused": False})[1]["paused"])
        self.assertTrue(self.call("/api/check-now")[1]["result"]["ok"])
        self.monitor.sender = lambda channels, msg: {name: "ok" for name in channels}
        self.assertEqual(self.call("/api/test-notify")[1]["result"], {"desktop": "ok"})

    def test_rejects_form_posts_from_other_sites(self):
        code, _ = self.post(b"interval_seconds=20", content_type="application/x-www-form-urlencoded")
        self.assertEqual(code, 415)


class StartupTests(unittest.TestCase):
    def test_second_copy_cannot_take_the_lock(self):
        from duo_restock import cli

        with tempfile.TemporaryDirectory() as tmp:
            first = cli.acquire_lock(Path(tmp) / "monitor.lock")
            self.assertIsNotNone(first)
            self.assertIsNone(cli.acquire_lock(Path(tmp) / "monitor.lock"))
            first.close()
            again = cli.acquire_lock(Path(tmp) / "monitor.lock")
            self.assertIsNotNone(again)
            again.close()

    def test_autostart_plist_restarts_only_after_a_crash(self):
        from duo_restock import autostart

        plist = autostart.build_plist(Path("/Users/me/duo/config.toml"), python="/usr/bin/python3")
        self.assertEqual(
            plist["ProgramArguments"],
            ["/usr/bin/python3", "-m", "duo_restock", "-c", "/Users/me/duo/config.toml",
             "run", "--log-file", "/Users/me/duo/monitor.log"],
        )
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue((Path(plist["WorkingDirectory"]) / "duo_restock").is_dir())


class NotifierTests(unittest.TestCase):
    def test_one_failing_channel_does_not_block_others(self):
        calls = []
        original = dict(notifiers.SENDERS)
        try:
            notifiers.SENDERS["webhook"] = lambda cfg, msg: (_ for _ in ()).throw(OSError("down"))
            notifiers.SENDERS["ntfy"] = lambda cfg, msg: calls.append(msg)
            results = notifiers.send_all(
                {"webhook": {}, "ntfy": {}, "nope": {}}, notifiers.Message("t", "b")
            )
        finally:
            notifiers.SENDERS.clear()
            notifiers.SENDERS.update(original)
        self.assertEqual(results["ntfy"], "ok")
        self.assertEqual(results["webhook"], "down")
        self.assertEqual(results["nope"], "unknown channel")
        self.assertEqual(len(calls), 1)

    def test_macos_notification_keeps_chinese_readable(self):
        from unittest import mock

        with mock.patch("platform.system", return_value="Darwin"), mock.patch("subprocess.run") as run:
            notifiers.desktop({}, notifiers.Message('🍏 有货了', 'Fifth "Ave" 有货'))
        script = run.call_args.args[0][2]
        self.assertIn('display notification "Fifth \\"Ave\\" 有货" with title "🍏 有货了"', script)
        self.assertNotIn("\\u", script)

    def test_missing_setting_is_reported(self):
        results = notifiers.send_all({"bark": {}}, notifiers.Message("t", "b"))
        self.assertIn("missing", results["bark"])


if __name__ == "__main__":
    unittest.main()
