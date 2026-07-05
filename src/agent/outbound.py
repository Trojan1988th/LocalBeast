r"""notify_user — the one canonical outbound path (W1.1, the proactivity brief).

Every the agent-initiated message to the user goes through here: the agent's tool,
the morning briefing cron, vitals reports, reminder fires. One door means one
place where the house rules live:

  1. SEASONS: the agent-initiated outbound is silenced during a quiet season
     (seasons.allows_outbound). the user-commissioned reminders and the watchdog
     are exempt — resting-by-choice and broken-by-accident must never look
     alike, and her meetings don't pause for her seasons.
  2. QUIET HOURS: non-urgent kinds hold overnight (default 22:30–07:00) and
     report themselves as deferred rather than sending a 3am ping. Reminders
     the user set herself fire regardless — she chose the time.
  3. LOG: every send (and every suppression) lands in outbound_log so the agent
     can remember what he's told her — and vitals can see failures.

Delivery is Telegram (bot token + chat id from .env), direct via httpx — no
tool-object coupling, so crons and the watchdog can import this with nothing
else loaded. Failure falls back to a desktop toast + counter bump.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("agent.outbound")

_DATA = Path(__file__).resolve().parents[2] / "data"
CONFIG_PATH = _DATA / "outbound_config.json"

_TZ = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "America/New_York"))

# Kinds that ignore quiet hours (urgency is inherent or the user-commissioned).
URGENT_KINDS = {"reminder", "watchdog", "urgent", "seasons-entry"}

_DEFAULT_CONFIG = {
    "quiet_start": "22:30",  # non-urgent outbound holds from here...
    "quiet_end": "07:00",    # ...until here (agent timezone)
    "enabled": True,          # master switch for ALL the agent-initiated outbound
}


def _config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def in_quiet_hours(now: datetime | None = None) -> bool:
    cfg = _config()
    now = now or datetime.now(_TZ)
    sh, sm = _parse_hhmm(cfg["quiet_start"])
    eh, em = _parse_hhmm(cfg["quiet_end"])
    minutes = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # wraps midnight


def _ensure_log_table() -> None:
    from .db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS outbound_log (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,   -- sent | suppressed_seasons | deferred_quiet | failed | disabled
                    detail TEXT
                )"""
            )


def _log(kind: str, message: str, status: str, detail: str = "") -> None:
    try:
        _ensure_log_table()
        from .db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO outbound_log (kind, message, status, detail) VALUES (%s,%s,%s,%s)",
                    (kind, message[:4000], status, detail[:500]),
                )
    except Exception as e:
        logger.warning("outbound: log write failed: %s", e)


def _telegram_send_text(text: str) -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def _toast(text: str) -> None:
    try:
        from .windows_tools import _run_ps
        safe = text.replace('"', "'").replace("`", "'")[:180]
        _run_ps(
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] | Out-Null\n"
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
            f"$xml.LoadXml('<toast><visual><binding template=\"ToastGeneric\">"
            f"<text>the agent</text><text>{safe}</text></binding></visual></toast>')\n"
            "$toast = New-Object Windows.UI.Notifications.ToastNotification($xml)\n"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier"
            "('the agent Agent').Show($toast)"
        )
    except Exception:
        pass


def notify_user(message: str, kind: str = "info", force: bool = False) -> dict:
    """Send the user a message through the canonical path.

    kind: "info" (default) | "reminder" | "watchdog" | "urgent" | "briefing"
          | "vitals" | "seasons-entry" | anything descriptive — reminders and
          watchdog are Seasons-exempt and quiet-hours-exempt by default.
    force: bypass quiet hours (NOT the Seasons gate — only the user's flag or an
           exempt kind passes that).

    Returns {sent: bool, status: str, detail: str}.
    """
    message = (message or "").strip()
    if not message:
        return {"sent": False, "status": "empty", "detail": "no message"}

    cfg = _config()
    if not cfg.get("enabled", True):
        _log(kind, message, "disabled")
        return {"sent": False, "status": "disabled",
                "detail": "outbound master switch is off (data/outbound_config.json)"}

    # Seasons gate — the house rule that matters most.
    from . import seasons
    if not seasons.allows_outbound(kind):
        _log(kind, message, "suppressed_seasons")
        logger.info("outbound: suppressed by Seasons (kind=%s)", kind)
        return {"sent": False, "status": "suppressed_seasons",
                "detail": "the user is in a quiet season; the agent-initiated outbound rests."}

    # Quiet hours — non-urgent messages wait for morning.
    if not force and kind not in URGENT_KINDS and in_quiet_hours():
        _log(kind, message, "deferred_quiet")
        return {"sent": False, "status": "deferred_quiet",
                "detail": f"quiet hours ({cfg['quiet_start']}–{cfg['quiet_end']}); "
                          "send it in the morning briefing instead, or use kind='urgent' "
                          "if it truly cannot wait."}

    ok, err = _telegram_send_text(message)
    if ok:
        _log(kind, message, "sent")
        return {"sent": True, "status": "sent", "detail": "telegram"}

    # Fallback: toast + failure counter, so a Telegram outage is neither
    # silent nor a lost message.
    logger.warning("outbound: telegram failed (%s) — falling back to toast", err)
    _toast(message)
    _log(kind, message, "failed", err)
    try:
        from .failures import bump
        bump("outbound_telegram")
    except Exception:
        pass
    return {"sent": False, "status": "failed", "detail": f"telegram: {err} (toast shown)"}


def recent_outbound(limit: int = 20) -> list[dict]:
    """What the agent has told the user lately (for his own memory + the briefing)."""
    try:
        _ensure_log_table()
        from .db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT created_at, kind, message, status FROM outbound_log "
                    "ORDER BY created_at DESC LIMIT %s", (limit,))
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
