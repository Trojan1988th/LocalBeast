"""Reflections tools: the evening nudge's eyes + the agent's own access."""
from __future__ import annotations

import logging

from langchain_core.tools import tool

logger = logging.getLogger("agent.reflections_tools")


@tool
def reflections_today() -> str:
    """
    Check today's Reflections state: has the user written an entry today? Is
    today a marked rest day? And, if a Books volume is tied to the practice
    (REFLECTIONS_BOOK_ID), where the user left off in it. Use this in the
    evening-nudge cron before deciding whether a gentle line is warranted —
    and never nudge on a rest day.
    """
    from .reflections import today_status
    s = today_status()
    lines = [
        f"Date: {s['date']}",
        f"Entry today: {'yes' if s['has_entry_today'] else 'no'}",
        f"Rest day: {'yes' if s['is_rest_day'] else 'no'}",
    ]
    if s.get("book_position"):
        lines.append(f"They're partway through: {s['book_position']}")
    return "\n".join(lines)


@tool
def reflections_recent(limit: int = 5) -> str:
    """
    Read the most recent Reflections entries (the user's and your notes
    beneath) — for continuity when they reference the practice, or when
    writing your note.

    Args:
        limit: How many recent entries (default 5).
    """
    from .reflections import _ensure_table
    from .db import get_connection
    _ensure_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM reflections ORDER BY entry_date DESC, id DESC "
                        "LIMIT %s", (min(int(limit), 20),))
            rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return "No reflections yet."
    out = []
    for r in rows:
        out.append(f"[{r['entry_date']}] {r['passage'] or '(nothing named)'}\n"
                   f"User: {r['user_text'][:400]}\n"
                   f"You: {(r['agent_text'] or '(no note)')[:300]}")
    return "\n\n".join(out)


REFLECTIONS_TOOLS = [reflections_today, reflections_recent]
