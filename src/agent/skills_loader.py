"""
Skills system: on-demand workflow guidance for the agent.

A "skill" is a markdown file at src/agent/skills/<name>/SKILL.md with YAML
frontmatter (name, description, optional triggers) and a body containing
actionable guidance for a specific task domain — e.g. journaling, memory
curation, morning briefings.

The skill INDEX (name + description per skill) is auto-included in every
system prompt so the agent always knows what's available. The skill BODY
is loaded on demand via the load_skill tool, becoming part of the active
context for the current turn and subsequent turns until it scrolls out
of the window naturally.

Skills can also be authored by the agent itself at runtime, stored under
src/agent/skills/agent_authored/<name>/SKILL.md. Curated skills (anywhere
else under skills/) are protected — agent-authored saves cannot overwrite
them.

Format:
    ---
    name: my_skill
    description: One-line description of when to use this skill.
    triggers: ["keyword1", "keyword2"]   # optional, informational
    ---

    # My Skill

    Body of actionable markdown guidance...
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_AGENT_AUTHORED_DIR = _SKILLS_DIR / "agent_authored"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n*(.*)\Z", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")


@dataclass
class SkillInfo:
    name: str
    description: str
    body: str
    triggers: list[str]
    path: Path
    is_agent_authored: bool


def _parse_skill_file(path: Path) -> SkillInfo | None:
    """Parse a SKILL.md file. Returns None if malformed or missing required fields."""
    try:
        text = path.read_text(encoding="utf-8").lstrip("﻿").strip()
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm_raw, body = m.group(1), m.group(2).strip()
    if not body:
        return None

    # Lightweight YAML — only need name/description/triggers. Avoids a yaml dep.
    fm: dict[str, object] = {}
    for line in fm_raw.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            items = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
            fm[key] = items
        else:
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            fm[key] = val

    name = str(fm.get("name") or path.parent.name).strip()
    description = str(fm.get("description") or "").strip()
    if not name or not description:
        return None
    triggers_raw = fm.get("triggers")
    triggers = list(triggers_raw) if isinstance(triggers_raw, list) else []

    is_authored = False
    try:
        is_authored = path.is_relative_to(_AGENT_AUTHORED_DIR)
    except AttributeError:
        # Python <3.9 fallback (unused in this codebase but harmless).
        is_authored = str(path).startswith(str(_AGENT_AUTHORED_DIR))

    return SkillInfo(
        name=name,
        description=description,
        body=body,
        triggers=triggers,
        path=path,
        is_agent_authored=is_authored,
    )


def discover_skills() -> list[SkillInfo]:
    """Return all skills under src/agent/skills/, sorted (curated first, then agent-authored)."""
    if not _SKILLS_DIR.exists():
        return []
    skills: list[SkillInfo] = []
    seen_names: set[str] = set()
    for skill_md in sorted(_SKILLS_DIR.rglob("SKILL.md")):
        info = _parse_skill_file(skill_md)
        if not info:
            continue
        if info.name in seen_names:
            # Curated skills win on name collision.
            continue
        seen_names.add(info.name)
        skills.append(info)
    skills.sort(key=lambda s: (s.is_agent_authored, s.name))
    return skills


def get_skill_by_name(name: str) -> SkillInfo | None:
    """Find a skill by exact name match."""
    for s in discover_skills():
        if s.name == name:
            return s
    return None


def format_skill_index(skills: Iterable[SkillInfo] | None = None) -> str:
    """Render the skill index for inclusion in the system prompt.

    Returns an empty string if no skills exist (caller should skip the section).
    """
    if skills is None:
        skills = discover_skills()
    skills = list(skills)
    if not skills:
        return ""
    lines = [
        "## Available Skills",
        "",
        "> Skills are on-demand workflow guidance for specific task domains. "
        "When a task matches a skill's description, call `load_skill(name)` "
        "BEFORE acting — the body will tell you the right sequence of tool "
        "calls and decisions. The body persists in your context for several "
        "turns after loading.",
        "",
    ]
    for s in skills:
        author_tag = " *(agent-authored)*" if s.is_agent_authored else ""
        lines.append(f"- **{s.name}**{author_tag}: {s.description}")
    return "\n".join(lines)


def save_agent_authored_skill(name: str, description: str, body: str) -> tuple[bool, str]:
    """Save a new agent-authored skill. Returns (success, message).

    Constraints:
    - name: lowercase alphanumeric + underscore, max 40 chars
    - description: 1-200 chars
    - body: at least 50 chars
    - cannot overwrite a curated skill (must use a different name)
    - re-saving an existing agent-authored skill is allowed (overwrite)
    """
    name = (name or "").strip()
    description = (description or "").strip()
    body = (body or "").strip()

    if not _NAME_RE.match(name):
        return False, "Invalid name. Use lowercase alphanumeric + underscore, max 40 chars."
    if not description:
        return False, "Description is required."
    if len(description) > 200:
        return False, f"Description too long ({len(description)} chars). Max 200."
    if len(body) < 50:
        return False, f"Body too short ({len(body)} chars). Min 50 chars of actionable guidance."

    for s in discover_skills():
        if s.name == name and not s.is_agent_authored:
            return False, f"Skill '{name}' is a curated skill and cannot be overwritten. Choose a different name."

    skill_dir = _AGENT_AUTHORED_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"

    content = f"""---
name: {name}
description: {description}
---

{body}
"""
    skill_md.write_text(content, encoding="utf-8")
    return True, f"Saved agent-authored skill '{name}'. It will appear in the next turn's skill index."
