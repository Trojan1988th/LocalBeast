"""Calendar tool: read-only eyes on the user's calendar (ICS subscription)."""
from __future__ import annotations

import logging

from langchain_core.tools import tool

logger = logging.getLogger("agent.calendar_tools")


@tool
def calendar_week(days: int = 7) -> str:
    """
    The user's calendar for the coming days (read-only ICS subscription). Use
    for a weekly planning brief, the morning briefing ("you have a thing at 2"),
    and when scheduling reminders around their real commitments.

    Args:
        days: How many days ahead to look (default 7).
    """
    from .calendar_ics import calendar_events
    events = calendar_events(days=min(int(days), 31))
    if events is None:
        return ("Calendar not configured. Add CALENDAR_ICS_URL to .env — for "
                "Google Calendar: Settings > your calendar > 'Secret address in "
                "iCal format'. Read-only; no OAuth needed.")
    if not events:
        return f"No events on the calendar in the next {days} days."
    lines = [f"Calendar, next {days} days ({len(events)} events):"]
    for e in events:
        start = e.get("start")
        when = (start.strftime("%a %b %d") if e.get("all_day")
                else start.strftime("%a %b %d, %I:%M %p")) if start else "?"
        loc = f" @ {e['location']}" if e.get("location") else ""
        lines.append(f"  {when} — {e.get('summary', '(untitled)')}{loc}")
    lines.append("(Recurring events may show only their next listed occurrence.)")
    return "\n".join(lines)


CALENDAR_TOOLS = [calendar_week]
