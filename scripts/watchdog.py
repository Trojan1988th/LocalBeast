r"""Dead-man's alarm (W0.2): the watcher that shares no failure domain.

Runs as its OWN Task Scheduler entry (AgentWatchdog, every 30 min) — a separate
process that imports nothing from the agent runtime. It reads files and the DB
directly, hits health endpoints directly, and alerts the user directly via the
Telegram Bot API (not through the agent's outbound module: if the API process is
the thing that died, the alarm must still ring).

Seasons-aware on PURPOSE and by the spec: the watchdog is EXEMPT from the
Seasons silence (resting-by-choice and broken-by-accident must never look
alike), but it reads seasons.json so it expects the slower keeper heartbeat
cadence instead of paging the user about a heartbeat that is resting correctly.

Alert hygiene: each distinct problem alerts at most once per 6h (state file),
so a down service is one ping, not 12. Recovery clears the memory.

Checks: heartbeat stamp freshness (season-adjusted) · API/Hindsight/Director/
Reader health · cron jobs enabled-but-erroring or overdue >26h · writes its
own stamp (data/watchdog_last.txt) so the vitals check watches the watcher.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import httpx  # noqa: E402

DATA = ROOT / "data"
LOG_PATH = ROOT / "logs" / "watchdog.log"
STATE_PATH = DATA / "watchdog_state.json"
STAMP_PATH = DATA / "watchdog_last.txt"

ALERT_COOLDOWN_H = 6

SERVICES = {
    "agent API (8000)": "http://127.0.0.1:8000/api/health",
    "Hindsight (8888)": "http://localhost:8888/health",
    "Director (5008)": "http://127.0.0.1:5008/health",
    "Reader (5005)": "http://127.0.0.1:5005/health",
}


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")


def telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": text}, timeout=30)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception as e:
        log(f"telegram failed: {e}")
        return False


def toast(text: str) -> None:
    try:
        import subprocess
        safe = text.replace('"', "'")[:180]
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] | Out-Null; "
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
            f"$xml.LoadXml('<toast><visual><binding template=\"ToastGeneric\">"
            f"<text>the agent watchdog</text><text>{safe}</text></binding></visual></toast>'); "
            "$t = New-Object Windows.UI.Notifications.ToastNotification($xml); "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier"
            "('the agent Watchdog').Show($t)"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30)
    except Exception:
        pass


def seasons_active_and_cadence() -> tuple[bool, float]:
    """Read seasons.json directly (no agent imports). Returns (active, expected
    heartbeat max age in hours)."""
    p = DATA / "seasons.json"
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        if s.get("active"):
            return True, float(s.get("keeper_cadence_hours", 168)) + 24
    except Exception:
        pass
    return False, 3.0  # normal cadence: hourly heartbeats, 3h grace


def check_heartbeat(problems: list[str]) -> None:
    active, max_age_h = seasons_active_and_cadence()
    stamp = DATA / "heartbeat_last.txt"
    try:
        age_h = (time.time() - float(stamp.read_text().strip())) / 3600
    except Exception:
        problems.append("heartbeat: no stamp file — scheduler may not be running at all")
        return
    if age_h > max_age_h:
        frame = " (keeper cadence — season active)" if active else ""
        problems.append(f"heartbeat: last cycle {age_h:.1f}h ago, expected within {max_age_h:.0f}h{frame}")


def check_services(problems: list[str]) -> None:
    # The dashboard middleware basic-auths everything including /api/health;
    # the watchdog authenticates like any other client (password from .env).
    auth = None
    pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if pw:
        auth = ("watchdog", pw)
    for name, url in SERVICES.items():
        try:
            r = httpx.get(url, timeout=6, auth=auth if "8000" in url else None)
            if r.status_code != 200:
                problems.append(f"{name}: HTTP {r.status_code}")
        except Exception:
            problems.append(f"{name}: unreachable")


def check_crons(problems: list[str]) -> None:
    """Direct DB read — enabled jobs that errored or look abandoned."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        url = os.environ.get("DATABASE_URL", "")
        if not url:
            return
        with psycopg.connect(url, row_factory=dict_row, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, status, last_run_at, last_run_status FROM cron_jobs")
                for r in cur.fetchall():
                    if r["status"] == "paused":
                        continue
                    if r["last_run_status"] == "error":
                        problems.append(f"cron '{r['name']}': last run errored")
                    elif r["last_run_at"] is not None:
                        age_h = (datetime.now(timezone.utc) - r["last_run_at"]).total_seconds() / 3600
                        if age_h > 8 * 24:
                            problems.append(f"cron '{r['name']}': enabled but silent {age_h/24:.0f}d")
    except Exception as e:
        problems.append(f"cron table unreachable: {type(e).__name__}")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"alerted": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> None:
    problems: list[str] = []
    check_heartbeat(problems)
    check_services(problems)
    check_crons(problems)

    state = load_state()
    alerted: dict = state.get("alerted", {})
    now = time.time()

    # New (or re-alertable) problems only — one ping per problem per cooldown.
    fresh = [p for p in problems
             if now - alerted.get(p, 0) > ALERT_COOLDOWN_H * 3600]

    if fresh:
        text = ("Watchdog: something in the house needs you.\n"
                + "\n".join(f"- {p}" for p in fresh)
                + "\n(This alarm is independent of the agent and exempt from Seasons "
                  "— rest and breakage must never look alike.)")
        sent = telegram(text)
        if not sent:
            toast("; ".join(fresh)[:180])
        for p in fresh:
            alerted[p] = now
        log(f"ALERT ({'telegram' if sent else 'toast'}): {fresh}")
    # Recovery: forget problems that no longer exist so they can re-alert later.
    state["alerted"] = {p: t for p, t in alerted.items() if p in problems}
    save_state(state)

    STAMP_PATH.write_text(str(now), encoding="utf-8")
    log(f"ok={not problems} problems={len(problems)}")


if __name__ == "__main__":
    main()
