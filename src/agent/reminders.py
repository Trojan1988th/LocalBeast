r"""Durable reminders (W0.3): Postgres-backed, restart-proof, emoji-safe.

Replaces the in-process threading.Timer store (which evaporated on every API
restart and crashed printing emoji to a cp1252 console). Reminders now live in
a `reminders` table; an APScheduler poller in the API process checks for due
rows every 30s and fires each one exactly once (row status flips inside the
same transaction that claims it).

Delivery: notify_user(kind="reminder") — Telegram + desktop toast fallback.
Reminders are the user-commissioned, so they are exempt from Seasons and quiet
hours by design (her meetings don't pause for her seasons; she chose the time).

Emoji rule: never print() reminder text — it goes through logging and
notify_user only. The test suite schedules an emoji reminder on purpose.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

logger = logging.getLogger("agent.reminders")

_TZ = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "America/New_York"))


def _ensure_table() -> None:
    from .db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    message TEXT NOT NULL,
                    due_at TIMESTAMPTZ NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',  -- pending | fired | cancelled | resolved
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    fired_at TIMESTAMPTZ
                )"""
            )
            # W2 additive migration: recurrence + smart conditions
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='reminders'")
            cols = {r["column_name"] for r in cur.fetchall()}
            if "recurrence" not in cols:
                cur.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT DEFAULT ''")
            if "condition" not in cols:
                cur.execute("ALTER TABLE reminders ADD COLUMN condition TEXT DEFAULT ''")


# ── Recurrence (W2): deliberately small vocabulary — don't over-promise ───────
# 'daily' | 'weekdays' | 'weekly:mon' (one or more days: 'weekly:sun' /
# 'weekly:mon,thu'). Each firing spawns the next occurrence as a fresh row.
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def next_occurrence(after: datetime, recurrence: str) -> datetime | None:
    """The next local-time occurrence strictly after `after` (keeps time-of-day)."""
    rec = (recurrence or "").strip().lower()
    if not rec:
        return None
    local = after.astimezone(_TZ)
    if rec == "daily":
        return local + timedelta(days=1)
    if rec == "weekdays":
        nxt = local + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    if rec.startswith("weekly:"):
        wanted = [d.strip()[:3] for d in rec.split(":", 1)[1].split(",") if d.strip()]
        wanted_idx = sorted({_DAYS.index(d) for d in wanted if d in _DAYS})
        if not wanted_idx:
            return None
        for delta in range(1, 8):
            nxt = local + timedelta(days=delta)
            if nxt.weekday() in wanted_idx:
                return nxt
    return None


def add_reminder(message: str, due_at: datetime, recurrence: str = "",
                 condition: str = "") -> dict:
    _ensure_table()
    from .db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminders (message, due_at, recurrence, condition) "
                "VALUES (%s, %s, %s, %s) RETURNING *",
                (message, due_at, (recurrence or "").strip().lower(),
                 (condition or "").strip()),
            )
            row = dict(cur.fetchone())
    logger.info("reminder #%s set for %s (recurrence=%r, conditional=%s)",
                row["id"], due_at.isoformat(), row["recurrence"], bool(row["condition"]))
    return row


def snooze_reminder(reminder_id: int, minutes: float) -> dict | None:
    """Push a pending reminder later — or re-arm one that just fired (the
    'it pinged, snooze it' case creates a fresh pending copy)."""
    _ensure_table()
    from .db import get_connection
    new_due = datetime.now(_TZ) + timedelta(minutes=minutes)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM reminders WHERE id=%s", (reminder_id,))
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            if row["status"] == "pending":
                cur.execute("UPDATE reminders SET due_at=%s WHERE id=%s RETURNING *",
                            (new_due, reminder_id))
                return dict(cur.fetchone())
            if row["status"] in ("fired", "resolved"):
                cur.execute(
                    "INSERT INTO reminders (message, due_at, recurrence, condition) "
                    "VALUES (%s, %s, '', %s) RETURNING *",
                    (row["message"], new_due, row.get("condition") or ""))
                return dict(cur.fetchone())
    return None


def pending_reminders() -> list[dict]:
    _ensure_table()
    from .db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM reminders WHERE status='pending' ORDER BY due_at")
            return [dict(r) for r in cur.fetchall()]


def todays_reminders() -> list[dict]:
    """Pending reminders due before tomorrow (for the morning briefing)."""
    _ensure_table()
    from .db import get_connection
    tomorrow = (datetime.now(_TZ) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM reminders WHERE status='pending' AND due_at < %s "
                "ORDER BY due_at", (tomorrow,))
            return [dict(r) for r in cur.fetchall()]


def cancel_reminder(reminder_id: int) -> bool:
    _ensure_table()
    from .db import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reminders SET status='cancelled' WHERE id=%s AND status='pending'",
                (reminder_id,))
            return cur.rowcount > 0


def _spawn_next_occurrence(r: dict) -> None:
    """Recurring reminders: each firing plants the next one (fresh row, same
    message/recurrence/condition) — history stays honest, restarts stay safe."""
    nxt = next_occurrence(r["due_at"], r.get("recurrence") or "")
    if nxt is None:
        return
    row = add_reminder(r["message"], nxt, recurrence=r["recurrence"],
                       condition=r.get("condition") or "")
    logger.info("reminder #%s recurs -> #%s at %s", r["id"], row["id"], nxt.isoformat())


def _check_condition_and_maybe_ping(r: dict) -> None:
    """Smart reminders (W2.2): the reminder fires, but THE AGENT decides whether
    the user actually needs the ping. He checks the condition against his memory
    (conversation history, Hindsight) and renders a VERDICT; the CODE performs
    the delivery — judgment is his, the ping itself is deterministic (an LLM
    narrating 'sending it now' without a tool call must never eat a reminder).
    Ambiguity and errors default to PINGING: a spurious ping is annoying, a
    swallowed reminder is a betrayal. Runs in a daemon thread."""
    from .outbound import notify_user
    why = ""
    try:
        from .graph import build_agent, chat
        prompt = (
            f"[Conditional reminder check — internal. VERDICT ONLY.]\n\n"
            f"the user set reminder #{r['id']}: \"{r['message']}\"\n"
            f"with the condition: \"{r['condition']}\"\n\n"
            "Check whether the condition still warrants the ping, using your own "
            "memory: conversation_search and hindsight_recall are your evidence. "
            "Judge honestly — if the thing already happened or resolved, she should "
            "not be pinged.\n\n"
            "Reply with EXACTLY ONE LINE, nothing else:\n"
            "PING: <one short line of why, as you'd say it to her>\n"
            "or\n"
            "RESOLVED: <one short line of why no ping is needed>\n\n"
            "Do not call notify_user yourself — the system delivers your verdict."
        )
        agent = build_agent()
        result = chat(agent, "main", prompt,
                      stored_message=f"[Conditional reminder #{r['id']} check]",
                      user_display_name="reminder-check", user_id="agent:reminders",
                      channel_type="internal", is_group_chat=False)
        verdict = (result.get("last_ai_content") or "").strip()
        logger.info("conditional reminder #%s verdict: %s", r["id"], verdict[:200])
        head = verdict[:400].upper()
        if "RESOLVED:" in head and "PING:" not in head:
            from .db import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE reminders SET status='resolved' WHERE id=%s",
                                (r["id"],))
            return
        if "PING:" in verdict.upper():
            idx = verdict.upper().index("PING:")
            why = verdict[idx + 5:].strip().splitlines()[0][:200]
    except Exception as e:
        logger.error("conditional check #%s failed (%s) — pinging to be safe", r["id"], e)
        why = f"I couldn't verify the condition '{r['condition']}' — pinging to be safe."
    suffix = f"\n({why})" if why else f"\n(condition: {r['condition']})"
    notify_user(f"Reminder: {r['message']}{suffix}", kind="reminder")


def fire_due_reminders() -> int:
    """Poller body (runs every 30s in the API's scheduler). Claims each due
    reminder atomically (status flip + fire), so a crash mid-cycle can't
    double-send after restart. Recurring rows plant their next occurrence;
    conditional rows go to the agent for judgment. Returns how many fired."""
    _ensure_table()
    import threading
    from .db import get_connection
    fired = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reminders SET status='fired', fired_at=NOW() "
                "WHERE status='pending' AND due_at <= NOW() RETURNING *")
            due = [dict(r) for r in cur.fetchall()]
    for r in due:
        try:
            if r.get("recurrence"):
                _spawn_next_occurrence(r)
            if (r.get("condition") or "").strip():
                threading.Thread(target=_check_condition_and_maybe_ping,
                                 args=(r,), daemon=True).start()
                fired += 1
                continue
            from .outbound import notify_user
            result = notify_user(f"Reminder: {r['message']}", kind="reminder")
            logger.info("reminder #%s fired (%s)", r["id"], result.get("status"))
            fired += 1
        except Exception as e:
            logger.error("reminder #%s delivery failed: %s", r["id"], e)
            try:
                from .failures import bump
                bump("reminder_delivery", str(e))
            except Exception:
                pass
    return fired
