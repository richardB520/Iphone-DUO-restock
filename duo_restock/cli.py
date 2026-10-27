"""Command line entry point: `python -m duo_restock <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
import threading

from . import __version__, apple, config, notifiers
from .monitor import Monitor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="duo-restock", description="iPhone Duo Apple Store 补货监控")
    parser.add_argument("-c", "--config", default="config.toml", help="配置文件路径 (默认 config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="持续监控，有货时推送")
    run.add_argument("--no-dashboard", action="store_true", help="不启动本地网页看板")
    sub.add_parser("check", help="只检查一次并打印结果")
    sub.add_parser("test-notify", help="向所有已配置的通知渠道发送一条测试消息")
    sub.add_parser("url", help="打印实际请求的 Apple 接口地址，方便在浏览器里排查")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2

    if args.command == "url":
        print(apple.fulfillment_url(cfg.region, cfg.parts, cfg.location, cfg.store, cfg.search_nearby))
        return 0

    if args.command == "test-notify":
        if not cfg.notify:
            print("没有启用任何 [notify.*] 渠道", file=sys.stderr)
            return 1
        msg = notifiers.Message(
            title="🍏 iPhone Duo 补货监控测试",
            body="如果你看到这条消息，说明通知配置正确。",
            url=f"{apple.site_base(cfg.region)}/shop/buy-iphone",
        )
        results = notifiers.send_all(cfg.notify, msg)
        for name, outcome in results.items():
            print(f"{name:12} {outcome}")
        return 0 if all(v == "ok" for v in results.values()) else 1

    monitor = Monitor(cfg)

    if args.command == "check":
        try:
            # A one-off check should report, not push.
            monitor.cfg.notify = {}
            monitor.poll_once()
        except apple.AppleError as e:
            print(f"检查失败: {e}", file=sys.stderr)
            return 1
        print_table(monitor.snapshot())
        return 0

    if not args.no_dashboard:
        from . import dashboard

        try:
            dashboard.serve(monitor, cfg.dashboard_host, cfg.dashboard_port)
        except OSError as e:
            logging.warning("dashboard not started (%s) - continuing without it", e)

    stop = threading.Event()
    try:
        monitor.run(stop)
    except KeyboardInterrupt:
        stop.set()
        print("\n已停止")
    return 0


def print_table(status: dict) -> None:
    if not status["pickup"]:
        print("附近没有返回任何门店数据（检查 part 型号号码和 location 是否正确）")
    for p in status["pickup"]:
        mark = "✅" if p["available"] else "  "
        print(f"{mark} {p['name']:<28} {p['store_name']:<24} {p['city']:<14} {p['quote']}")
    for d in status["delivery"]:
        print(f"📦 {d['name']:<28} 送货: {d['quote'] or '—'}")


if __name__ == "__main__":
    sys.exit(main())
