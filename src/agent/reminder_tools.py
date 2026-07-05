"""Reminder tools — durable, restart-proof (W0.3 rework).

Previously threading.Timer in-process: reminders evaporated on every API
restart and the fire path crashed printing emoji to a cp1252 console. Now
backed by the Postgres `reminders` table (src/agent/reminders.py); a poller
in the API's scheduler fires due rows via notify_user (Telegram + toast
fallback). Reminders are the user-commissioned: they fire through Seasons and
quiet hours by design.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_TZ = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "America/New_York"))


@tool
def set_reminder(message: str, minutes: float) -> str:
    """
    Set a reminder that reaches the user at the right moment — Telegram on her
    phone (with a desktop toast as fallback). Reminders are DURABLE: they
    survive restarts, and they fire even during quiet hours or a quiet season,
    because the user commissioned them herself.

    Args:
        message: What the reminder should say. Be specific and actionable.
                 Emoji are fine.
        minutes: How many minutes from now to fire (fractional OK; no upper
                 limit — durable storage means "in 3 days" works now).
    """
    if minutes <= 0:
        return "Error: minutes must be greater than 0."
    from .reminders import add_reminder
    due = datetime.now(_TZ) + timedelta(minutes=minutes)
    row = add_reminder(message, due)
    return (f"Reminder #{row['id']} set for {due.strftime('%a %b %d, %I:%M %p')}: "
            f"'{message}' (durable — survives restarts).")


@tool
def schedule_reminder(message: str, when_iso: str, recurrence: str = "",
                      condition: str = "") -> str:
    """
    Schedule a reminder for a specific date/time — the full Ministry of
    Reminders. YOU are the natural-language parser: when the user says "remind me
    Thursday 2pm about the IEP meeting", convert that to when_iso yourself
    (America/New_York local time) and confirm back in one line.

    Durable (survives restarts) and fires even during quiet hours or a quiet
    season — reminders are the user-commissioned, not your outreach.

    Args:
        message: What the reminder should say. Specific, actionable, emoji fine.
        when_iso: First/next firing time as ISO local datetime, e.g.
                  "2026-07-09T14:00:00". For relative asks ("in 3 hours"),
                  compute it from the current time.
        recurrence: "" (one-shot, default) | "daily" | "weekdays" |
                    "weekly:sun" or "weekly:mon,thu" ("every Sunday evening" →
                    weekly:sun at her stated time). Each firing plants the next.
        condition: Optional SMART condition — only for things checkable against
                   your own memory ("if I haven't heard back from Matt",
                   "unless I already sent the form"). When it fires, YOU check
                   memory and decide: ping with the why, or quietly resolve.
                   Don't accept conditions you can't check (weather, email,
                   other apps) — offer a plain reminder instead.
    """
    from .reminders import add_reminder
    try:
        due = datetime.fromisoformat(when_iso)
        if due.tzinfo is None:
            due = due.replace(tzinfo=_TZ)
    except ValueError:
        return f"Error: when_iso {when_iso!r} is not a valid ISO datetime."
    if due < datetime.now(_TZ) - timedelta(minutes=1):
        return f"Error: {due.strftime('%a %b %d, %I:%M %p')} is in the past."
    rec = (recurrence or "").strip().lower()
    if rec:
        from .reminders import next_occurrence
        if next_occurrence(due, rec) is None:
            return (f"Error: recurrence {recurrence!r} not recognized — use "
                    "'daily', 'weekdays', or 'weekly:<days>' (e.g. weekly:sun).")
    row = add_reminder(message, due, recurrence=rec, condition=condition)
    bits = [f"Reminder #{row['id']} set for {due.strftime('%a %b %d, %I:%M %p')}"]
    if rec:
        bits.append(f"repeats {rec}")
    if condition.strip():
        bits.append(f"smart — I'll check \"{condition.strip()}\" before pinging")
    return f"{', '.join(bits)}: '{message}'"


@tool
def snooze_reminder(reminder_id: int, minutes: float) -> str:
    """
    Snooze a reminder: push a pending one later, or re-arm one that just fired
    ("snooze that 20 minutes"). See list_reminders for ids.

    Args:
        reminder_id: The #id to snooze.
        minutes: How many minutes from NOW it should fire instead.
    """
    from .reminders import snooze_reminder as _snooze
    row = _snooze(reminder_id, minutes)
    if row is None:
        return f"No reminder #{reminder_id} found (or it was cancelled)."
    due_local = row["due_at"].astimezone(_TZ)
    return f"Reminder #{row['id']} snoozed to {due_local.strftime('%a %b %d, %I:%M %p')}: '{row['message']}'"


@tool
def list_reminders() -> str:
    """
    List all pending reminders (durable — includes ones set before restarts),
    with recurrence and smart conditions shown.
    """
    from .reminders import pending_reminders
    rows = pending_reminders()
    if not rows:
        return "No pending reminders."
    lines = [f"Pending reminders ({len(rows)}):"]
    for r in rows:
        due_local = r["due_at"].astimezone(_TZ)
        extra = []
        if r.get("recurrence"):
            extra.append(f"repeats {r['recurrence']}")
        if (r.get("condition") or "").strip():
            extra.append(f"if: {r['condition']}")
        suffix = f"  ({'; '.join(extra)})" if extra else ""
        lines.append(f"  #{r['id']} — {due_local.strftime('%a %b %d, %I:%M %p')}: {r['message']}{suffix}")
    return "\n".join(lines)


@tool
def cancel_reminder(reminder_id: int) -> str:
    """
    Cancel a pending reminder by its id (see list_reminders for ids).

    Args:
        reminder_id: The #id of the reminder to cancel.
    """
    from .reminders import cancel_reminder as _cancel
    if _cancel(reminder_id):
        return f"Reminder #{reminder_id} cancelled."
    return f"No pending reminder #{reminder_id} (already fired, cancelled, or never existed)."
