r"""Self-vitals (W0.5): the agent audits his own health and reports honestly.

collect() runs every check programmatically and returns structured results;
the `self_vitals` tool exposes it to the agent (a weekly cron has him run it and
tell the user the truth via notify_user). The July 2026 capability audit found
three failures the agent should have caught himself — this module is how he
catches the fourth.

Expected scheduled behaviors live in data/vitals_manifest.json — every new
proactive feature registers there (the proactivity brief ground rule) so nothing
added later can die silently either. The manifest maps name → max age in
hours before the behavior counts as stale.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("agent.vitals")

_ROOT = Path(__file__).resolve().parents[2]
_DATA = _ROOT / "data"
MANIFEST_PATH = _DATA / "vitals_manifest.json"
_TZ = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "America/New_York"))

# name → {kind, max_age_hours, ...}; seasons-aware entries adjust automatically.
_DEFAULT_MANIFEST = {
    "heartbeat": {"kind": "stamp", "path": "data/heartbeat_last.txt",
                  "max_age_hours": 3, "seasons_max_age_hours": 192},
    "watchdog": {"kind": "stamp", "path": "data/watchdog_last.txt", "max_age_hours": 2},
    "cron:Daily Summary": {"kind": "cron", "name": "Daily Summary", "max_age_hours": 26},
    "cron:Daily Highlight": {"kind": "cron", "name": "Daily Highlight", "max_age_hours": 26},
    "cron:Morning Briefing": {"kind": "cron", "name": "Morning Briefing", "max_age_hours": 26,
                              "seasons_exempt": True},
    "cron:Weekly Self-Vitals": {"kind": "cron", "name": "Weekly Self-Vitals",
                                "max_age_hours": 192, "seasons_exempt": True},
}

_SERVICES = {
    "agent_api": "http://127.0.0.1:8000/api/health",
    "hindsight": "http://localhost:8888/health",
    "director": "http://127.0.0.1:5008/health",
    "reader": "http://127.0.0.1:5005/health",
}


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(_DEFAULT_MANIFEST, indent=2), encoding="utf-8")
    return dict(_DEFAULT_MANIFEST)


def _stamp_age_hours(path: Path) -> float | None:
    try:
        return (time.time() - float(path.read_text().strip())) / 3600
    except Exception:
        try:
            return (time.time() - path.stat().st_mtime) / 3600
        except Exception:
            return None


def collect() -> dict:
    """All checks. Never raises — a vitals check that crashes is its own
    worst finding. Structure: {ok: bool, problems: [...], sections: {...}}."""
    problems: list[str] = []
    sections: dict = {}

    # Seasons context (adjusts expectations, reported honestly)
    seasons_active = False
    try:
        from . import seasons
        seasons_active = seasons.is_active()
        sections["seasons"] = {"active": seasons_active}
    except Exception as e:
        sections["seasons"] = {"error": str(e)}

    # 1. Scheduled behaviors vs manifest
    behaviors = {}
    manifest = load_manifest()
    cron_rows = {}
    try:
        from .db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name, status, last_run_at, last_run_status, "
                            "last_run_error FROM cron_jobs")
                for r in cur.fetchall():
                    cron_rows[r["name"]] = dict(r)
    except Exception as e:
        problems.append(f"cron table unreadable: {e}")

    for key, spec in manifest.items():
        max_age = spec.get("max_age_hours", 26)
        if seasons_active and spec.get("seasons_max_age_hours"):
            max_age = spec["seasons_max_age_hours"]
        if seasons_active and spec.get("seasons_exempt"):
            behaviors[key] = {"status": "resting (season)"}
            continue
        if spec.get("kind") == "stamp":
            age = _stamp_age_hours(_ROOT / spec["path"])
            if age is None:
                behaviors[key] = {"status": "NO STAMP"}
                problems.append(f"{key}: never stamped ({spec['path']})")
            elif age > max_age:
                behaviors[key] = {"status": f"STALE {age:.1f}h (max {max_age}h)"}
                problems.append(f"{key}: stale — last ran {age:.1f}h ago")
            else:
                behaviors[key] = {"status": f"ok ({age:.1f}h ago)"}
        elif spec.get("kind") == "cron":
            row = cron_rows.get(spec["name"])
            if not row:
                behaviors[key] = {"status": "JOB MISSING"}
                problems.append(f"{key}: cron job not found")
                continue
            if row.get("status") == "paused":
                behaviors[key] = {"status": "paused"}
                continue
            last = row.get("last_run_at")
            if last is None:
                behaviors[key] = {"status": "never ran"}
                problems.append(f"{key}: enabled but never ran")
                continue
            age = (datetime.now(last.tzinfo) - last).total_seconds() / 3600
            if row.get("last_run_status") == "error":
                behaviors[key] = {"status": f"LAST RUN ERRORED: {row.get('last_run_error', '')[:120]}"}
                problems.append(f"{key}: last run errored")
            elif age > max_age:
                behaviors[key] = {"status": f"STALE {age:.1f}h"}
                problems.append(f"{key}: hasn't run in {age:.1f}h")
            else:
                behaviors[key] = {"status": f"ok ({age:.1f}h ago)"}
    sections["scheduled_behaviors"] = behaviors

    # Any enabled cron whose last run errored (even unmanifested — W0.4 rule)
    for name, row in cron_rows.items():
        if row.get("status") != "paused" and row.get("last_run_status") == "error":
            note = f"cron '{name}': last run errored: {str(row.get('last_run_error'))[:120]}"
            if note not in problems and not any(name in p for p in problems):
                problems.append(note)

    # 2. Services
    services = {}
    _pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    for name, url in _SERVICES.items():
        try:
            _auth = ("vitals", _pw) if (_pw and "8000" in url) else None
            r = httpx.get(url, timeout=6, auth=_auth)
            services[name] = "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
            if r.status_code != 200:
                problems.append(f"service {name}: HTTP {r.status_code}")
        except Exception as e:
            services[name] = "DOWN"
            problems.append(f"service {name}: unreachable ({type(e).__name__})")
    sections["services"] = services

    # 3. Memory actually retaining? (newest Hindsight-bound message vs today)
    try:
        from .db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(created_at) AS m FROM messages")
                newest = cur.fetchone()["m"]
        sections["messages_newest"] = str(newest)
    except Exception as e:
        problems.append(f"messages table unreadable: {e}")

    # 4. Daily summaries freshness
    try:
        from .db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(summary_date) AS m FROM daily_summaries")
                newest = cur.fetchone()["m"]
        if newest:
            age_days = (datetime.now(_TZ).date() - newest).days
            sections["daily_summaries"] = f"newest {newest} ({age_days}d old)"
            if age_days > 2:
                problems.append(f"daily summaries stale: newest is {newest}")
    except Exception as e:
        sections["daily_summaries"] = f"unreadable: {e}"

    # 5. Failure counters (last 7 days)
    try:
        from .failures import since
        recent = since(24 * 7)
        sections["recent_failures"] = recent
        for name, entry in recent.items():
            problems.append(f"failure counter '{name}': {entry.get('count')} total, "
                            f"latest: {entry.get('last_detail', '')[:100]}")
    except Exception:
        pass

    # 6. Outbound health (recent failed sends)
    try:
        from .db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM outbound_log "
                            "WHERE status='failed' AND created_at > NOW() - INTERVAL '7 days'")
                failed = cur.fetchone()["c"]
        sections["outbound_failed_7d"] = failed
        if failed:
            problems.append(f"{failed} outbound message(s) failed to send this week")
    except Exception:
        sections["outbound_failed_7d"] = "n/a"

    return {"ok": not problems, "problems": problems, "sections": sections,
            "checked_at": datetime.now(_TZ).isoformat()}
