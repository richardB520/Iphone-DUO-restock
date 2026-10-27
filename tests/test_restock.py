import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from duo_restock import apple, config, notifiers
from duo_restock.monitor import Monitor

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pickup_us.json").read_text())
PARTS = ["MTEST1LL/A", "MTEST2LL/A"]


def make_config(tmp: str, **monitor) -> config.Config:
    return config.from_dict(
        {
            "apple": {"region": "us", "location": "10001"},
            "products": [
                {"part": "MTEST1LL/A", "name": "Duo 256 Black", "url": "https://example.com/buy"},
                {"part": "MTEST2LL/A", "name": "Duo 512 White"},
            ],
            "monitor": {"state_file": "state.json", **monitor},
            "notify": {"webhook": {"url": "https://example.com/hook"}},
        },
        base_dir=Path(tmp),
    )


def payload_with(first_available: bool, delivery: str = "Delivers Jan 25 &ndash; Feb 1") -> dict:
    data = json.loads(json.dumps(FIXTURE))
    store = data["body"]["content"]["pickupMessage"]["stores"][0]
    store["partsAvailability"]["MTEST1LL/A"]["pickupDisplay"] = "available" if first_available else "unavailable"
    data["body"]["content"]["deliveryMessage"]["MTEST1LL/A"]["regular"]["deliveryOptionMessages"][0][
        "displayName"
    ] = delivery
    return data


class FakeApple:
    def __init__(self, *payloads):
        self.payloads = list(payloads)

    def __call__(self, **kwargs):
        return apple.parse(self.payloads.pop(0), kwargs["parts"])


class ParseTests(unittest.TestCase):
    def test_parses_pickup_per_store_and_part(self):
        result = apple.parse(FIXTURE, PARTS)
        self.assertEqual(len(result.pickup), 4)
        first = result.pickup[0]
        self.assertEqual(first.key, "MTEST1LL/A@R095")
        self.assertTrue(first.available)
        self.assertEqual(first.quote, "Available Today")
        self.assertEqual(first.product, "iPhone Duo 256GB Black")
        self.assertFalse(any(a.available for a in result.pickup[1:]))

    def test_parses_delivery_and_unescapes_entities(self):
        result = apple.parse(FIXTURE, PARTS)
        self.assertEqual(len(result.delivery), 1)
        self.assertTrue(result.delivery[0].buyable)
        self.assertEqual(result.delivery[0].quote, "Delivers Jan 25 – Feb 1")

    def test_ignores_unrequested_parts(self):
        result = apple.parse(FIXTURE, ["MTEST2LL/A"])
        self.assertEqual({a.part for a in result.pickup}, {"MTEST2LL/A"})

    def test_bad_head_status_raises_throttled_error(self):
        with self.assertRaises(apple.AppleError) as ctx:
            apple.parse({"head": {"status": "541"}, "body": {}}, PARTS)
        self.assertTrue(ctx.exception.throttled)

    def test_empty_body_yields_nothing(self):
        result = apple.parse({"head": {"status": "200"}, "body": {"content": {}}}, PARTS)
        self.assertEqual(result.pickup, [])
        self.assertEqual(result.delivery, [])


class UrlTests(unittest.TestCase):
    def test_location_query(self):
        url = apple.fulfillment_url("us", PARTS, location="10001")
        self.assertTrue(url.startswith("https://www.apple.com/shop/fulfillment-messages?"))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["parts.0"], ["MTEST1LL/A"])
        self.assertEqual(q["parts.1"], ["MTEST2LL/A"])
        self.assertEqual(q["location"], ["10001"])

    def test_store_query_and_regions(self):
        url = apple.fulfillment_url("cn", ["MX1CH/A"], store="R705")
        self.assertTrue(url.startswith("https://www.apple.com.cn/shop/"))
        self.assertIn("store=R705", url)
        self.assertIn("searchNearby=true", url)
        self.assertTrue(apple.fulfillment_url("hk", ["X"], location="x").startswith("https://www.apple.com/hk/shop/"))

    def test_chinese_location_is_encoded(self):
        url = apple.fulfillment_url("cn", ["X"], location="上海 上海 黄浦区")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["location"], ["上海 上海 黄浦区"])

    def test_requires_location_or_store(self):
        with self.assertRaises(ValueError):
            apple.fulfillment_url("us", PARTS)


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = config.load(Path(__file__).parent.parent / "config.example.toml")
        self.assertEqual(cfg.region, "us")
        self.assertEqual(len(cfg.products), 2)
        self.assertEqual(list(cfg.notify), ["desktop"])

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
        self.assertIn("Duo 256 Black", self.sent[0].body)
        self.assertEqual(self.sent[0].url, "https://example.com/buy")
        self.assertEqual(m.poll_once(), [])  # still available: no repeat
        self.assertEqual(m.poll_once(), [])  # sold out: silent by default
        self.assertEqual(len(self.sent), 1)

    def test_first_check_alerts_when_already_in_stock(self):
        m = self.monitor(FakeApple(payload_with(True)))
        self.assertEqual([e["kind"] for e in m.poll_once()], ["in_stock"])

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

    def test_delivery_quote_change_alerts_after_baseline(self):
        m = self.monitor(FakeApple(payload_with(False), payload_with(False, "Delivers Tomorrow")))
        self.assertEqual(m.poll_once(), [])
        events = m.poll_once()
        self.assertEqual([e["kind"] for e in events], ["delivery_changed"])
        self.assertIn("Delivers Tomorrow", self.sent[0].body)

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

    def test_backoff_grows_and_caps(self):
        m = self.monitor(FakeApple())
        m.failures = 1
        self.assertEqual(m.next_delay(), 120)
        m.failures = 10
        self.assertEqual(m.next_delay(), 900)


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

    def test_missing_setting_is_reported(self):
        results = notifiers.send_all({"bark": {}}, notifiers.Message("t", "b"))
        self.assertIn("missing", results["bark"])


if __name__ == "__main__":
    unittest.main()
