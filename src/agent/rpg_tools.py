"""RPG main-chat tool (S5): the one-way bridge from normal chat into the story
memory bank. Registered in MAIN chat only in spirit — it refuses to run inside
a story thread (in-story recall is already scoped by the RPG route), so
campaigns can be asked about from normal conversation but never ambush a story."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def story_recall(query: str, story: str = "") -> str:
    """Recall what happened in an RPG campaign (the DM/story memory bank).

    Use this when the user asks about a tabletop story you've been running for
    them from NORMAL chat — "how's our campaign going?", "what did I decide about
    the baron?", "remind me where we left the Clockwork Manor". Story memories
    live in a separate bank from normal chat, so this is the ONLY way to reach
    them from here.

    Args:
        query: what you want to recall (a topic, name, event, or decision).
        story: optional story slug to scope the recall to one campaign
               (e.g. "clockwork-manor"). Leave empty to search across all stories.

    Not available while actively playing a story — in that context the DM already
    recalls the current story automatically.
    """
    from .graph import _thread_ctx  # lazy: avoid import cycle at module load
    from .hindsight import recall
    from .rpg import RPG_BANK_ID

    thread_id = getattr(_thread_ctx, "thread_id", "") or ""
    if thread_id.startswith("rpg:"):
        return ("story_recall is not available inside a story thread — you already "
                "recall this campaign's memory automatically while playing.")
    tags = [f"rpg:{story.strip()}"] if story.strip() else None
    try:
        text = recall(RPG_BANK_ID, query, tags=tags, tags_match="all" if tags else "any")
    except Exception as e:
        return f"Story recall failed: {e}"
    return text or "No story memories found for that query."
