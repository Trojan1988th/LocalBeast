"""
Hindsight integration: deep memory that learns.

Retains every user/assistant exchange as lived experience.
Agent can recall and reflect via tools.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("agent.hindsight")

HINDSIGHT_BASE_URL = os.environ.get("HINDSIGHT_BASE_URL", "http://localhost:8888")
HINDSIGHT_BANK_ID = os.environ.get("HINDSIGHT_BANK_ID", "stateful-agent")
HINDSIGHT_ENABLED = os.environ.get("HINDSIGHT_ENABLED", "true").lower() in ("true", "1", "yes")
# User ID tag for Hindsight memory (e.g. user:your_id_here). Used when retaining.
HINDSIGHT_USER_ID = os.environ.get("HINDSIGHT_USER_ID", "").strip()


def _get_client():
    """Lazy-import and create Hindsight client."""
    try:
        from hindsight_client import Hindsight
        return Hindsight(base_url=HINDSIGHT_BASE_URL)
    except ImportError:
        return None


def _format_as_lived_experience(
    user_content: str,
    assistant_content: str | None,
    user_display_name: str | None = None,
) -> str:
    """
    Format a user/assistant exchange as the AI's lived experience.
    Not bullet points — narrative, first-person, experiential.
    """
    user_content = (user_content or "").strip()
    assistant_content = (assistant_content or "").strip() if assistant_content else None
    who = user_display_name or "The user"

    if assistant_content:
        return (
            f"{who} and I were in conversation. They said to me: \"{user_content}\" "
            f"I responded from our shared context: \"{assistant_content}\""
        )
    return f"{who} reached out to me. They said: \"{user_content}\""


def retain_exchange(
    bank_id: str,
    user_content: str,
    assistant_content: str | None = None,
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
    user_display_name: str | None = None,
    channel_type: str | None = None,
    is_group_chat: bool = False,
    extra_tags: list[str] | None = None,
) -> bool:
    """
    Retain a user/assistant exchange into Hindsight as lived experience.
    Returns True if retained, False if Hindsight unavailable or disabled.

    Tags applied:
    - user:{user_id} - Stable identity (discord_id, telegram_id, or local_name)
    - channel:{discord|telegram|local} - Platform/source identifier
    - group - Applied if is_group_chat is True
    - extra_tags - Caller-supplied additions (e.g. ["reflections"] or a story
      thread id + act for RPG banks)
    """
    if not HINDSIGHT_ENABLED:
        return False

    client = _get_client()
    if not client:
        return False

    content = _format_as_lived_experience(user_content, assistant_content, user_display_name)
    effective_bank = bank_id or HINDSIGHT_BANK_ID

    try:
        metadata: dict[str, Any] = {}
        if thread_id:
            metadata["thread_id"] = thread_id

        # Build tags for cross-platform continuity
        tags: list[str] = []

        # Primary user identity tag (prefer passed user_id, fallback to env)
        effective_user_id = (user_id or HINDSIGHT_USER_ID).strip()
        if effective_user_id:
            # Ensure consistent format: user:{id}
            user_tag = effective_user_id if ":" in effective_user_id else f"user:{effective_user_id}"
            tags.append(user_tag)

        # Channel/platform tag
        if channel_type:
            tags.append(f"channel:{channel_type.lower()}")

        # Group chat tag
        if is_group_chat:
            tags.append("group")

        # Caller-supplied extra tags (e.g. story thread + act for RPG banks)
        if extra_tags:
            tags.extend(extra_tags)

        with client:
            client.retain(
                bank_id=effective_bank,
                content=content,
                context="conversation",
                timestamp=datetime.now(timezone.utc).isoformat(),
                metadata=metadata if metadata else None,
                tags=tags if tags else None,
            )
        return True
    except Exception:
        return False


def recall(
    bank_id: str,
    query: str,
    tag_groups: list | None = None,
    tags: list[str] | None = None,
    tags_match: str = "any",
) -> str:
    """
    Recall memories from Hindsight. Returns formatted string of relevant memories.
    When Hindsight returns results, format them as lived experience — not bullet points.

    tags/tags_match scope the recall (e.g. tags=["rpg:my-story"], tags_match="all"
    limits results to one campaign inside a story bank).
    """
    client = _get_client()
    if not client:
        return "Hindsight is not available. Memory recall failed."

    effective_bank = bank_id or HINDSIGHT_BANK_ID

    try:
        with client:
            response = client.recall(
                bank_id=effective_bank,
                query=query,
                tag_groups=tag_groups,
                tags=tags or None,
                tags_match=tags_match,
            )
        results = getattr(response, "results", []) or []
        if not results:
            return "I don't have any memories that match that."

        # Format as lived recollection — narrative, not bullet list
        texts = []
        for r in results:
            text = getattr(r, "text", None) or (str(r) if r else None)
            if text and isinstance(text, str) and text.strip():
                texts.append(text.strip())

        if not texts:
            return "I don't have any memories that match that."

        return "From my experience with the user:\n\n" + "\n\n".join(texts)
    except Exception as e:
        return f"Hindsight recall failed: {e}"


def recall_with_privacy_flag(
    bank_id: str,
    query: str,
    tag_groups: list | None = None,
    tags: list[str] | None = None,
    tags_match: str = "any",
    exclude_private: bool = False,
) -> tuple[str, bool]:
    """
    Like recall(), but also returns whether any result was tagged 'private'.

    When exclude_private=True, private-tagged results are filtered out CLIENT-SIDE
    after the server returns them — a hard guarantee independent of any server-side
    tag filter.

    Returns:
        (formatted_text, has_private)
        - formatted_text: the same string recall() would return (empty string if no results)
        - has_private: True if any memory included in formatted_text was private-tagged.
    """
    client = _get_client()
    if not client:
        return "Hindsight is not available. Memory recall failed.", False

    effective_bank = bank_id or HINDSIGHT_BANK_ID

    try:
        with client:
            response = client.recall(
                bank_id=effective_bank,
                query=query,
                tag_groups=tag_groups,
                tags=tags or None,
                tags_match=tags_match,
            )
        results = getattr(response, "results", []) or []
        if not results:
            return "", False

        texts = []
        has_private = False
        filtered_count = 0
        for r in results:
            result_tags = getattr(r, "tags", None) or []
            if "private" in result_tags:
                if exclude_private:
                    filtered_count += 1
                    continue  # Hard client-side filter: never include private results
                has_private = True  # only True when a private-tagged row is actually shown
            text = getattr(r, "text", None) or (str(r) if r else None)
            if text and isinstance(text, str) and text.strip():
                texts.append(text.strip())

        if filtered_count:
            logger.info(
                "Hindsight recall: filtered out %d private-tagged results "
                "(exclude_private=%s)", filtered_count, exclude_private,
            )

        if not texts:
            return "", False

        return "From my experience with the user:\n\n" + "\n\n".join(texts), has_private
    except Exception as e:
        return f"Hindsight recall failed: {e}", False


def reflect(bank_id: str, query: str) -> str:
    """
    Reflect on memories — deeper synthesis, patterns, insights.
    Use for relational questions, pattern-based questions, or self-reflection.
    """
    client = _get_client()
    if not client:
        return "Hindsight is not available. Reflection failed."

    effective_bank = bank_id or HINDSIGHT_BANK_ID

    try:
        with client:
            answer = client.reflect(bank_id=effective_bank, query=query)
        text = getattr(answer, "text", None) or (str(answer) if answer else None)
        return (text or "").strip() or "I reflected but have nothing specific to share."
    except Exception as e:
        return f"Hindsight reflect failed: {e}"
