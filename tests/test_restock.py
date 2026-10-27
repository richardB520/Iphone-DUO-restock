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
        self.assertEqual(m.poll_once(), [])  # sold out: silent by default
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

    def test_run_sleeps_outside_active_hours_without_polling(self):
        from datetime import datetime

        class OneShot(threading.Event):
            def wait(self, timeout=None):
                self.waited = timeout
                self.set()
                return True

        m = self.monitor(FakeApple(), active_hours=["05:00", "10:00"])
        self.now = datetime(2026, 10, 27, 3, 0).timestamp()
        stop = OneShot()
        m.run(stop)
        self.assertEqual(stop.waited, 60)
        self.assertEqual(m.snapshot()["checks"], 0)
        self.assertEqual(m.snapshot()["sleeping_until"], datetime(2026, 10, 27, 5, 0).timestamp())

    def test_block_cooldowns_then_error_backoff(self):
        m = self.monitor(FakeApple())
        m.blocks = 1
        self.assertEqual(m.next_delay(), 300)
        m.blocks = 3
        self.assertEqual(m.next_delay(), 1200)
        m.blocks = 9
        self.assertEqual(m.next_delay(), 1800)
        m.blocks, m.failures = 0, 1
        self.assertEqual(m.next_delay(), 120)
        m.failures = 10
        self.assertEqual(m.next_delay(), 1800)


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
