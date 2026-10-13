"""Urgent flags reach you as a Windows notification (and, if you add a bot token to .env, a private Telegram
message). Each flag is sent once; the notifications table remembers which."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from xml.sax.saxutils import escape

from applier.paths import Paths

# Windows PowerShell's own app id, so the notification needs nothing installed.
_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:APPLIER_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show($toast)
"""


def toast(title: str, text: str) -> bool:
    """Show one Windows notification. Returns False if Windows refused (nothing else happens)."""
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{escape(title[:80])}</text>"
           f"<text>{escape(text[:250])}</text></binding></visual></toast>")
    env = dict(os.environ, APPLIER_TOAST_XML=xml)  # passed by environment: no quoting problems
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", _TOAST], env=env, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def telegram(text: str) -> bool:
    """Optional: only if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are filled in .env. The token is never logged."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat or "paste" in token:
        return False
    import httpx

    try:
        response = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": text[:3500]}, timeout=20)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def notify_urgent(conn: sqlite3.Connection, paths: Paths, send=toast) -> int:
    """Send each open urgent flag once. Returns how many were sent."""
    rows = conn.execute("""SELECT f.* FROM flags f LEFT JOIN notifications n ON n.flag_id = f.id
                           WHERE f.status = 'open' AND f.severity = 'urgent' AND n.flag_id IS NULL
                           ORDER BY f.created_at""").fetchall()
    sent = 0
    for flag in rows:
        title = "Job search: " + flag["kind"].replace("_", " ")
        shown = send(title, flag["message"])
        telegram(f"{title}\n{flag['message']}")
        if shown:
            with conn:
                conn.execute("INSERT OR IGNORE INTO notifications (flag_id) VALUES (?)", (flag["id"],))
            sent += 1
    return sent
