"""macOS login item: start the monitor at login and restart it if it dies."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.duo-restock.monitor"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(config_path: Path, python: str = sys.executable) -> dict:
    config_path = config_path.resolve()
    folder = config_path.parent
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python, "-m", "duo_restock", "-c", str(config_path),
            "run", "--log-file", str(folder / "monitor.log"),
        ],
        # The package is run from the project folder, not installed.
        "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
        "RunAtLoad": True,
        # Restart after a crash, but not after a clean exit (e.g. another copy is running).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": str(folder / "monitor.out.log"),
        "StandardErrorPath": str(folder / "monitor.out.log"),
    }


def install(config_path: Path) -> int:
    if sys.platform != "darwin":
        print("自动启动目前只支持 macOS", file=sys.stderr)
        return 1
    if not config_path.exists():
        print(f"找不到配置文件 {config_path}", file=sys.stderr)
        return 1
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    with path.open("wb") as f:
        plistlib.dump(build_plist(config_path), f)
    result = subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"launchctl 加载失败：{result.stderr.strip()}", file=sys.stderr)
        return 1
    print("已设置：登录后自动启动监控，意外退出会自动重启。")
    print("取消：python3 -m duo_restock uninstall-autostart")
    return 0


def uninstall() -> int:
    path = plist_path()
    if not path.exists():
        print("没有设置过自动启动")
        return 0
    subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
    path.unlink()
    print("已取消自动启动，监控也已停止。")
    return 0
