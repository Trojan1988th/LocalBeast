"""LangChain tools for the skills system."""
from __future__ import annotations

from langchain_core.tools import tool

from .skills_loader import (
    discover_skills,
    format_skill_index,
    get_skill_by_name,
    save_agent_authored_skill,
)


@tool
def list_skills() -> str:
    """
    List all available skills with their descriptions.

    The skill index is also auto-included in your system prompt, so most
    of the time you don't need to call this. Call it explicitly when:
      - You suspect a new skill was added since the conversation started
      - You want to refresh after authoring a new skill via `save_skill`
      - You want to verify a skill exists before calling `load_skill`

    Returns a markdown bullet list of skill names + descriptions.
    """
    return format_skill_index() or "(no skills available)"


@tool
def load_skill(name: str) -> str:
    """
    Load the full body of a skill into your context.

    Use this when a task matches a skill description in the index. The
    body contains step-by-step workflow guidance — read it carefully and
    follow the steps. The body persists in your context until it scrolls
    out of the message window naturally (~30 messages).

    Best practice: call `load_skill` ONCE near the start of a task that
    matches a skill, not repeatedly. The body is now in your context;
    you don't need to reload it on subsequent turns of the same task.

    Args:
        name: Exact skill name from the index (e.g. "journal_workflow").

    Returns the skill's full markdown body, or an error message if not found.
    """
    info = get_skill_by_name(name)
    if not info:
        return (
            f"Skill '{name}' not found. Call `list_skills()` to see what's "
            f"available — names are case-sensitive."
        )
    author_tag = " (agent-authored)" if info.is_agent_authored else ""
    return f"# Skill: {info.name}{author_tag}\n\n{info.body}"


@tool
def save_skill(name: str, description: str, body: str) -> str:
    """
    Save a new agent-authored skill that you've identified is worth capturing.

    Use this when you discover a workflow pattern that worked well and
    that future-you (or future autonomous cycles) should reuse. Common
    triggers: a sequence of tool calls + decisions you've made several
    times, an anti-pattern worth avoiding, a heuristic that resolved a
    tricky case.

    Saved skills go to `skills/agent_authored/<name>/SKILL.md` and appear
    in the skill index from the next turn onward. They cannot overwrite
    curated skills (those live elsewhere under `skills/` and are protected).

    Constraints:
    - name: lowercase alphanumeric + underscore, max 40 chars (e.g. `weekly_review`)
    - description: 1-200 chars; THIS IS WHAT FUTURE-YOU SEES — be specific
      about WHEN to use the skill, not just what it is
    - body: at least 50 chars of actionable markdown; numbered steps work well

    Args:
        name: Skill identifier (e.g. "weekly_review").
        description: One-line summary used in the skill index. Specific WHEN > vague WHAT.
        body: Full markdown guidance with numbered steps where possible.

    Returns success message or validation error.
    """
    ok, msg = save_agent_authored_skill(name, description, body)
    return msg


SKILLS_TOOLS = [list_skills, load_skill, save_skill]
