r"""Calendar visibility: read-only ICS subscription.

Set CALENDAR_ICS_URL in .env — for Google Calendar, the "Secret address in
iCal format" (Settings > your calendar > Integrate calendar) needs no OAuth.
Cached to data/calendar_cache.ics and refreshed at most hourly; parsed with a
deliberately small VEVENT reader (DTSTART/DTEND/SUMMARY — recurring-event
expansion is noted as a limitation, not faked). Feeds the weekly planning
brief, the morning briefing, and reminder scheduling.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("agent.calendar")

_TZ = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "America/New_York"))
_CACHE = Path(__file__).resolve().parents[2] / "data" / "calendar_cache.ics"
_CACHE_MAX_AGE_S = 3600


def _ics_url() -> str:
    return os.environ.get("CALENDAR_ICS_URL", "").strip()


def _fetch_ics() -> str | None:
    """Cached ICS text; refresh at most hourly. None if unconfigured/unreachable."""
    url = _ics_url()
    if not url:
        return None
    if _CACHE.exists() and (time.time() - _CACHE.stat().st_mtime) < _CACHE_MAX_AGE_S:
        return _CACHE.read_text(encoding="utf-8", errors="replace")
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(r.text, encoding="utf-8")
        return r.text
    except Exception as e:
        logger.warning("calendar: ICS fetch failed (%s) — using stale cache if any", e)
        if _CACHE.exists():
            return _CACHE.read_text(encoding="utf-8", errors="replace")
        return None


def _unfold(text: str) -> list[str]:
    """RFC5545 line unfolding (continuation lines start with space/tab)."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse_dt(value: str, params: str) -> datetime | None:
    """DTSTART value → aware datetime (date-only events land at 00:00 local)."""
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):  # all-day
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=_TZ)
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=ZoneInfo("UTC")).astimezone(_TZ)
        dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        tzid = None
        m = re.search(r"TZID=([^;:]+)", params)
        if m:
            tzid = m.group(1)
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid) if tzid else _TZ).astimezone(_TZ)
        except Exception:
            return dt.replace(tzinfo=_TZ)
    except ValueError:
        return None


def calendar_events(days: int = 7) -> list[dict] | None:
    """Events in the next `days` days. None = calendar not configured.
    Limitation (honest): recurring events (RRULE) are shown only on their
    first occurrence — most calendar exports expand recurring events, so in
    practice this is rare; noted rather than half-implemented."""
    text = _fetch_ics()
    if text is None:
        return None
    now = datetime.now(_TZ)
    horizon = now + timedelta(days=days)
    events: list[dict] = []
    cur: dict | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            start = cur.get("start")
            if start and now - timedelta(hours=12) <= start <= horizon:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, value = line.split(":", 1)
            name, _, params = key.partition(";")
            if name == "DTSTART":
                cur["start"] = _parse_dt(value, params)
                cur["all_day"] = bool(re.fullmatch(r"\d{8}", value.strip()))
            elif name == "DTEND":
                cur["end"] = _parse_dt(value, params)
            elif name == "SUMMARY":
                cur["summary"] = value.strip()
            elif name == "LOCATION" and value.strip():
                cur["location"] = value.strip()
    events.sort(key=lambda e: e.get("start") or now)
    return events
