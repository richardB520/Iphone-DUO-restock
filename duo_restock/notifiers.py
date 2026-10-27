"""Push channels: Bark, ServerChan, PushPlus, Telegram, ntfy, Discord, webhook, email, desktop."""

from __future__ import annotations

import json
import logging
import platform
import shutil
import smtplib
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class Message:
    title: str
    body: str
    url: str = ""


Sender = Callable[[dict, Message], None]


def _post_json(url: str, payload: dict, headers: dict | None = None) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _post_form(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def bark(cfg: dict, msg: Message) -> None:
    # cfg: url = "https://api.day.app/<device key>"
    payload = {
        "title": msg.title,
        "body": msg.body,
        "level": cfg.get("level", "timeSensitive"),
        "sound": cfg.get("sound", "alarm"),
        "group": "iPhone Duo",
    }
    if msg.url:
        payload["url"] = msg.url
    _post_json(cfg["url"].rstrip("/"), payload)


def serverchan(cfg: dict, msg: Message) -> None:
    # WeChat push via https://sct.ftqq.com
    body = msg.body + (f"\n\n[立即购买]({msg.url})" if msg.url else "")
    _post_form(f"https://sctapi.ftqq.com/{cfg['sendkey']}.send", {"title": msg.title, "desp": body})


def pushplus(cfg: dict, msg: Message) -> None:
    body = msg.body.replace("\n", "<br>")
    if msg.url:
        body += f'<br><a href="{msg.url}">立即购买</a>'
    _post_json("https://www.pushplus.plus/send", {"token": cfg["token"], "title": msg.title, "content": body})


def telegram(cfg: dict, msg: Message) -> None:
    text = f"*{msg.title}*\n{msg.body}" + (f"\n{msg.url}" if msg.url else "")
    _post_json(
        f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
        {"chat_id": cfg["chat_id"], "text": text, "parse_mode": "Markdown"},
    )


def ntfy(cfg: dict, msg: Message) -> None:
    server = cfg.get("server", "https://ntfy.sh").rstrip("/")
    payload = {
        "topic": cfg["topic"],
        "title": msg.title,
        "message": msg.body,
        "priority": int(cfg.get("priority", 5)),
        "tags": ["iphone"],
    }
    if msg.url:
        payload["click"] = msg.url
    _post_json(server, payload)


def discord(cfg: dict, msg: Message) -> None:
    content = f"**{msg.title}**\n{msg.body}" + (f"\n{msg.url}" if msg.url else "")
    _post_json(cfg["webhook_url"], {"content": content})


def webhook(cfg: dict, msg: Message) -> None:
    _post_json(cfg["url"], {"title": msg.title, "body": msg.body, "url": msg.url}, cfg.get("headers"))


def email(cfg: dict, msg: Message) -> None:
    em = EmailMessage()
    em["Subject"] = msg.title
    em["From"] = cfg.get("from", cfg["username"])
    em["To"] = cfg["to"]
    em.set_content(msg.body + (f"\n\n{msg.url}" if msg.url else ""))
    port = int(cfg.get("port", 465))
    if port == 465:
        server = smtplib.SMTP_SSL(cfg["host"], port, timeout=20)
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=20)
        server.starttls()
    with server:
        server.login(cfg["username"], cfg["password"])
        server.send_message(em)


def _applescript_str(text: str) -> str:
    # json.dumps would turn Chinese into \uXXXX escapes, which AppleScript prints literally.
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell_str(text: str) -> str:
    # Single-quoted PowerShell strings expand nothing; only ' needs doubling.
    return "'" + text.replace("'", "''") + "'"


def desktop(cfg: dict, msg: Message) -> None:
    sys.stdout.write("\a")
    sys.stdout.flush()
    system = platform.system()
    if system == "Darwin":
        script = (
            f"display notification {_applescript_str(msg.body)} "
            f"with title {_applescript_str(msg.title)} sound name \"Glass\""
        )
        subprocess.run(["osascript", "-e", script], check=False)
        if cfg.get("say"):
            subprocess.run(["say", msg.title], check=False)
    elif system == "Linux" and shutil.which("notify-send"):
        subprocess.run(["notify-send", "-u", "critical", msg.title, msg.body], check=False)
    elif system == "Windows":
        ps = (
            "[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true;"
            f"$n.ShowBalloonTip(10000, {_powershell_str(msg.title)}, {_powershell_str(msg.body)}, 'Info');"
            "Start-Sleep -Seconds 10"
        )
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])


SENDERS: dict[str, Sender] = {
    "bark": bark,
    "serverchan": serverchan,
    "pushplus": pushplus,
    "telegram": telegram,
    "ntfy": ntfy,
    "discord": discord,
    "webhook": webhook,
    "email": email,
    "desktop": desktop,
}


def send_all(channels: dict[str, dict], msg: Message) -> dict[str, str]:
    """Send `msg` to every configured channel; one failing channel never blocks the rest."""
    results: dict[str, str] = {}
    for name, cfg in channels.items():
        sender = SENDERS.get(name)
        if sender is None:
            log.warning("unknown notify channel [notify.%s] - skipped", name)
            results[name] = "unknown channel"
            continue
        try:
            sender(cfg, msg)
            results[name] = "ok"
        except KeyError as e:
            log.error("[notify.%s] is missing setting %s", name, e)
            results[name] = f"missing {e}"
        except Exception as e:  # noqa: BLE001 - report and keep going
            log.error("[notify.%s] failed: %s", name, e)
            results[name] = str(e)
    return results
