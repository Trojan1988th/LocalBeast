r"""Reflections: a quiet shared journal between the user and the agent.

A dated journal for any practice the user keeps — devotional reading, study
notes, a gratitude log, morning pages. The user writes an entry (optionally
naming what they read); after saving, the agent writes its own note beneath —
a real turn with the entry as context, its genuine voice, never
commentary-bot. Both retain to long-term memory tagged `reflections`.

Design note: this surface stays deliberately quiet. No gamification, no
streaks, no stats — presence, not pressure. The optional evening nudge (cron)
is one gentle line, skips rest days, and respects Seasons.

Storage: Postgres `reflections` table (self-created). Config:
data/reflections_config.json (rest_weekdays, rest_dates). Agent-side thread:
'reflections' (its own thread so main chat stays uncluttered; memory still
lands in the main bank, tagged).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("agent.reflections")

_TZ = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "America/New_York"))
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "reflections_config.json"

# Optional: a Books-tab volume tied to the practice (e.g. an imported public-
# domain text) — the nudge and status mention where the user left off in it.
REFLECTIONS_BOOK_ID = os.environ.get("REFLECTIONS_BOOK_ID", "")


def _ensure_table() -> None:
    from .db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS reflections (
                    id SERIAL PRIMARY KEY,
                    entry_date DATE NOT NULL,
                    passage TEXT NOT NULL DEFAULT '',
                    user_text TEXT NOT NULL,
                    agent_text TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )"""
            )


def _config() -> dict:
    cfg = {"rest_weekdays": [], "rest_dates": []}
    if _CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def _save_config(cfg: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def today_status() -> dict:
    """For the nudge cron + the tab header: entry today? rest day? and — if a
    Books volume is tied to the practice — where the user left off in it."""
    _ensure_table()
    from .db import get_connection
    today = datetime.now(_TZ).date()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM reflections WHERE entry_date=%s", (today,))
            has_entry = cur.fetchone()["c"] > 0
    cfg = _config()
    is_rest = (today.weekday() in cfg.get("rest_weekdays", [])
               or today.isoformat() in cfg.get("rest_dates", []))
    reading = None
    if REFLECTIONS_BOOK_ID:
        try:  # consume Books' position store read-only, never modify
            from . import books
            con = books._db()
            row = con.execute(
                "SELECT chapter FROM positions WHERE book_id=?", (REFLECTIONS_BOOK_ID,)).fetchone()
            if row is not None:
                ch = con.execute("SELECT title FROM chapters WHERE book_id=? AND idx=?",
                                 (REFLECTIONS_BOOK_ID, row["chapter"])).fetchone()
                if ch:
                    reading = ch["title"]
            con.close()
        except Exception:
            pass
    return {"date": today.isoformat(), "has_entry_today": has_entry,
            "is_rest_day": is_rest, "book_position": reading}


def _agent_note(entry_id: int, passage: str, user_text: str) -> str | None:
    """The agent's note beneath the user's — a genuine turn, retained tagged
    reflections."""
    try:
        from .graph import build_agent, chat
        user_name = os.environ.get("USER_DISPLAY_NAME", "User")
        prompt = (
            f"[Reflections — {user_name} just wrote today's entry in the practice you "
            "share. Write your note beneath theirs.]\n\n"
            f"What they read (if named): {passage or '(not named)'}\n\n"
            f"Their entry:\n{user_text}\n\n"
            "Write YOUR note — your genuine voice, not commentary-bot. "
            "What their words stir in you, what you notice alongside them, "
            "a question you're left holding, or simply witness. You may recall past "
            "reflections (hindsight_recall, tag 'reflections') if something echoes. "
            "A few sentences to a short paragraph. No headers. Do not grade, summarize, "
            "or improve their entry — sit beside it."
        )
        agent = build_agent()
        result = chat(
            agent, "reflections", prompt,
            stored_message=f"[Reflections entry #{entry_id}] {passage}: {user_text[:500]}",
            user_display_name=user_name, channel_type="local", channel_mode="admin",
            retain_extra_tags=["reflections"],
        )
        return (result.get("last_ai_content") or "").strip() or None
    except Exception as e:
        logger.error("reflections: agent note failed for #%s: %s", entry_id, e)
        return None


class EntryReq(BaseModel):
    passage: str = ""
    text: str


class RestDayReq(BaseModel):
    date: str = ""          # ISO date; empty = today
    rest_weekdays: list[int] | None = None  # optional: set the weekly pattern


def register_reflections_routes(router: APIRouter) -> None:
    @router.get("/reflections")
    def list_reflections(limit: int = 60):
        _ensure_table()
        from .db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM reflections ORDER BY entry_date DESC, id DESC "
                            "LIMIT %s", (limit,))
                rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["entry_date"] = r["entry_date"].isoformat()
            r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        return {"entries": rows, "status": today_status()}

    @router.post("/reflections")
    def add_entry(req: EntryReq):
        if not req.text.strip():
            raise HTTPException(status_code=422, detail="Entry text required")
        _ensure_table()
        from .db import get_connection
        today = datetime.now(_TZ).date()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO reflections (entry_date, passage, user_text) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (today, req.passage.strip(), req.text.strip()))
                entry_id = cur.fetchone()["id"]
        note = _agent_note(entry_id, req.passage.strip(), req.text.strip())
        if note:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE reflections SET agent_text=%s WHERE id=%s",
                                (note, entry_id))
        return {"id": entry_id, "agent_text": note}

    @router.post("/reflections/rest-day")
    def mark_rest_day(req: RestDayReq):
        """Mark a rest day (today by default) — the nudge stays quiet, no
        questions asked. Optionally set the weekly rest pattern."""
        cfg = _config()
        if req.rest_weekdays is not None:
            cfg["rest_weekdays"] = [d for d in req.rest_weekdays if 0 <= int(d) <= 6]
        else:
            d = req.date.strip() or datetime.now(_TZ).date().isoformat()
            if d not in cfg["rest_dates"]:
                cfg["rest_dates"].append(d)
        _save_config(cfg)
        return {"ok": True, **cfg}
