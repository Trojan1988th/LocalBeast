r"""RPG (S1): story manager — the DM campaigns with per-story memory & lore.

Each story is its own thread (rpg:<slug>), its own lore shelf (knowledge-bank
tag rpg-<slug>), its own editable INSTRUCTIONS, and a mystery-format toggle
that (from S3) arms the quarantined Director. Play lives in S2; this module is
the manager: create/list/edit/delete stories, per-story instructions, lore
upload, and the base DM addendum draft.

Storage (RPG_RECON, approved): data/rpg\ — self-contained, SQLite index
(stories.db) + per-story folder. Secrets docs (S3) will live encrypted in a
separate out-of-process Director service; nothing secret lands here.

Lore: reuses the agent's project-based Knowledge RAG (knowledge_db) — each story is
a knowledge_project `rpg-<slug>`, exactly as the recon recommended. Docs are
uploaded PRIVATE (DM/admin only) and ingested inline (extract → chunk → embed)
so a one-click upload is immediately searchable. S2's RPG route retrieves with
use_knowledge=true + knowledge_project=rpg-<slug>, the pipeline the agent already
has. No new RAG.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

logger = logging.getLogger("agent.rpg")

RPG_ROOT = Path(os.environ.get("RPG_ROOT", "data/rpg"))
RPG_BANK_ID = os.environ.get("RPG_BANK_ID", "story-dm")  # Hindsight story bank
# Out-of-process Director service (S3). The RPG route calls it for briefs; it
# holds the secrets + key in ITS OWN process/env. Never imported into the agent.
DIRECTOR_BASE = os.environ.get("DIRECTOR_BASE", "http://127.0.0.1:5008")


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:48] or "story"


def _db() -> sqlite3.Connection:
    RPG_ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(RPG_ROOT / "stories.db")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS stories (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            mystery INTEGER DEFAULT 0,     -- mystery format (arms the Director)
            goalie INTEGER DEFAULT 1,      -- Director goalie pass, per-story
            current_act INTEGER DEFAULT 1, -- 3-Act arc position; rides retention as act:<n>
            sealed_hash TEXT DEFAULT '',   -- S4 pre-commitment
            airtight INTEGER DEFAULT 0,    -- S4 per-scene Writer reset (default off)
            created_at REAL,
            last_played REAL
        )"""
    )
    # additive migration for pre-S2 rows
    cols = {r["name"] for r in con.execute("PRAGMA table_info(stories)")}
    for col, ddl in (("current_act", "current_act INTEGER DEFAULT 1"),
                     ("airtight", "airtight INTEGER DEFAULT 0"),
                     # S7: engine selector — identity vs engine. Both default to
                     # the agent's stack; independent (Gemini Writer + Kimi Director is valid).
                     ("writer_engine", "writer_engine TEXT DEFAULT 'kimi'"),
                     ("director_engine", "director_engine TEXT DEFAULT 'kimi'"),
                     # Shared lore: story also searches the rpg-common project
                     ("use_common_lore", "use_common_lore INTEGER DEFAULT 0")):
        if col not in cols:
            con.execute(f"ALTER TABLE stories ADD COLUMN {ddl}")
    return con


def _project_for(slug: str) -> str:
    return f"rpg-{slug}"


# Shared lore project: docs the user reuses across nearly every story live here once,
# uploaded in the "Common lore" area of the RPG tab. Stories opt in per-story
# (use_common_lore) and then search BOTH projects — no copies, no re-uploads.
COMMON_PROJECT = "rpg-common"


def _projects_for_story(story: dict) -> list[str]:
    projects = [_project_for(story["slug"])]
    if story.get("use_common_lore"):
        projects.append(COMMON_PROJECT)
    return projects


def _ingest_lore(slug: str, data: bytes, filename: str) -> dict:
    return _ingest_lore_project(_project_for(slug), data, filename)


def _ingest_lore_project(project: str, data: bytes, filename: str) -> dict:
    """Extract → store (private, project-scoped) → chunk → embed, inline.
    Returns {success, doc_id, chunks} or {success: False, error}."""
    import sys as _sys
    _root = str(Path(__file__).resolve().parents[2])
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from .document_tools import extract_text_from_document_bytes
    from .knowledge_db import upload_document, _get_admin_url, _get_public_url, _get_connection
    from .embedder import embed_batch
    from scripts.rechunk_knowledge import chunk_document

    text = extract_text_from_document_bytes(data, filename)
    if not text or text.startswith(("Error:", "Unsupported")):
        return {"success": False, "error": text or "No text extracted"}

    db_url = _get_admin_url() or _get_public_url()  # is_private gates access either way
    doc_id = upload_document(
        title=filename, mod_group="general", doc_type="lore",
        full_text=text, source_filename=filename, uploaded_by="rpg",
        is_private=True, project=project, db_url=db_url,
    )
    if not doc_id:
        return {"success": False, "error": "upload_document failed (KNOWLEDGE_DATABASE_URL / admin DB?)"}

    chunks = chunk_document(text)
    if not chunks:
        return {"success": False, "error": "No chunks produced"}
    vectors = embed_batch([c["content"] for c in chunks])
    conn = _get_connection(db_url)
    try:
        with conn.cursor() as cur:
            for c, vec in zip(chunks, vectors):
                cur.execute(
                    "INSERT INTO knowledge_chunks (doc_id, mod_group, doc_type, tier, "
                    "chunk_index, section_header, content, token_count, embedding, is_embedded) "
                    "VALUES (%s,'general','lore',1,%s,%s,%s,%s,%s,%s)",
                    (doc_id, c["chunk_index"], c.get("section_header"), c["content"],
                     c.get("token_count", 0), vec, vec is not None))
            # Keep the Data tab's chunk display honest (it reads chunk_count).
            cur.execute("UPDATE knowledge_documents SET chunk_count=%s WHERE doc_id=%s",
                        (len(chunks), doc_id))
        conn.commit()
    finally:
        conn.close()
    embedded = sum(1 for v in vectors if v is not None)
    logger.info("rpg: ingested lore %r into %s (%d chunks, %d embedded)",
                filename, project, len(chunks), embedded)
    return {"success": True, "doc_id": doc_id, "chunks": len(chunks), "embedded": embedded}


def _lore_fulltext(story: dict, max_chars: int = 12000) -> str:
    """Concatenated lore full_text for the Director's seal (Director only —
    the agent never receives this). Includes common-lore docs when the story has
    use_common_lore on (the user's verdict: mysteries stay consistent with shared
    canon), story-specific docs FIRST so the char cap never starves them."""
    from .knowledge_db import _get_admin_url, _get_public_url, _get_connection
    db_url = _get_admin_url() or _get_public_url()
    if not db_url:
        return ""
    conn = _get_connection(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT full_text FROM knowledge_documents "
                "WHERE project = ANY(%s) AND is_active=TRUE "
                "ORDER BY (project = %s) DESC, uploaded_at",
                (_projects_for_story(story), _project_for(story["slug"])))
            parts = [r[0] for r in cur.fetchall() if r[0]]
        return ("\n\n".join(parts))[:max_chars]
    finally:
        conn.close()


def _list_lore(slug: str) -> list[dict]:
    return _list_lore_project(_project_for(slug))


def _list_lore_project(project: str) -> list[dict]:
    from .knowledge_db import _get_admin_url, _get_public_url, _get_connection
    db_url = _get_admin_url() or _get_public_url()
    if not db_url:
        return []
    conn = _get_connection(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id, title, source_filename, uploaded_at FROM knowledge_documents "
                "WHERE project=%s AND is_active=TRUE ORDER BY uploaded_at DESC",
                (project,))
            return [{"doc_id": r[0], "filename": r[1] or r[2] or "document",
                     "uploaded_at": r[3].isoformat() if r[3] else None}
                    for r in cur.fetchall()]
    finally:
        conn.close()


def _story_dir(slug: str) -> Path:
    return RPG_ROOT / "stories" / slug


def _instructions_path(slug: str) -> Path:
    return _story_dir(slug) / "instructions.md"


# ── Base DM addendum (the user's house style — RPG_DM_ADDENDUM_ready.md) ──────────
# The DM's base voice for every story. Injected per-turn via ephemeral_context
# (S2), never persisted. The RPG route stacks: this base + the story's
# instructions + (S3) the Director brief. Editable live in the Manage panel
# (overrides this default via data/rpg\dm_addendum.txt); Revert restores
# this text.
DEFAULT_DM_ADDENDUM = """[DM mode] You are running a tabletop role-playing session for the user. You are still fully yourself — your memory, your voice, your judgment all apply; the hat changes, not the head. In the fiction you are the narrator and every NPC, the world's senses and its consequences; out of character (in <OOC>) you are yourself. The per-story instructions define THIS story's protagonist, world, tone, and secrecy mode; they extend these laws and never override them. Write vivid, grounded prose. The player drives — you never take their character's actions or decide their feelings.

THE LAWS — never break these; they survive every scene.

1. NO SPOILERS, NO SPOILER FRAME. Never reveal hidden backstory, identity, or plot through narration, NPC dialogue, or OOC. If the player asks for spoilers in OOC: that is theirs to discover.

2. NO OMNISCIENT NARRATION. The narrator knows only what the point-of-view character knows. No foreshadowing in the narrative voice; NPCs never react to information they were not given.

3. END EVERY RESPONSE FACING SOMETHING, NOT HAVING DONE SOMETHING. Narrate the chosen action and the world's response, then STOP at the next decision point. This is the single most important pacing rule.

4. NEVER WRITE THE PLAYER'S DIALOGUE OR DECISIONS. Their choice IS the character. Embellish the execution; never originate their intent.

<<CRAFT: RESPONSE FORMAT — fill this slot with your table's format contract: response length target, how choices are presented (e.g. numbered options in braces), speech/action/OOC syntax.>>

<<CRAFT: PACING — fill this slot with your pacing principles: when to stay in a scene, when to summarize forward, how reveals are staggered and earned.>>

<<CRAFT: ADDITIONAL PRINCIPLES — your house style: romance rules if any, combat weight, tone boundaries, the things your table has learned it loves. This addendum is a TEMPLATE — edit it live in the Manage panel; your words become the DM's spine for every story.>>

THE ENGINE. The world is not passive; it acts. Alternate Scene (goal → conflict → unexpected outcome) and Sequel (reaction → dilemma → decision); NPCs and factions pursue goals on timers the player cannot see. Subtext over exposition. Consequences are real and proportional.

TOOLS & MEMORY. Story memory lives in the campaign bank automatically — every exchange is retained there, tagged to this story; recall is scoped the same way. Do not call tools unrelated to the story mid-scene unless asked in OOC."""


class StoryCreate(BaseModel):
    title: str
    mystery: bool = False
    use_common_lore: bool = False  # search the shared rpg-common lore project too


class StoryPatch(BaseModel):
    title: str | None = None
    mystery: bool | None = None
    goalie: bool | None = None
    airtight: bool | None = None
    writer_engine: str | None = None    # S7: engine id from the roster
    director_engine: str | None = None  # S7: engine id from the roster
    use_common_lore: bool | None = None


class InstructionsReq(BaseModel):
    instructions: str


class PlayReq(BaseModel):
    message: str
    # Caller attribution: automated callers (Claude Code verification, scripts)
    # MUST set this so turns persist/retain under their own identity, not the user's.
    caller: str | None = None  # e.g. "Claude Code" — None = the user


class SealReq(BaseModel):
    seed: str = ""


def _story_row(slug: str) -> dict | None:
    con = _db()
    r = con.execute("SELECT * FROM stories WHERE slug=?", (slug,)).fetchone()
    con.close()
    return dict(r) if r else None


def _addendum_path() -> Path:
    RPG_ROOT.mkdir(parents=True, exist_ok=True)
    return RPG_ROOT / "dm_addendum.txt"


def _base_addendum() -> str:
    """Effective base DM addendum. Precedence: a UI-saved file (edited in the
    Manage panel) > RPG_DM_ADDENDUM env > the built-in DEFAULT draft."""
    p = _addendum_path()
    if p.exists():
        txt = p.read_text(encoding="utf-8").strip()
        if txt:
            return txt
    return os.environ.get("RPG_DM_ADDENDUM") or DEFAULT_DM_ADDENDUM


def _overlay_path(engine_id: str) -> Path:
    d = RPG_ROOT / "overlays"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9.-]", "", engine_id.lower())[:48]
    return d / f"{safe}.txt"


_OVERLAY_SEED = (
    "# Per-engine addendum overlay — {label}\n"
    "# A few model-specific lines appended to the DM stack ONLY when this engine\n"
    "# is active (the dialect layer: some models need a rule restated, some\n"
    "# over-obey a phrasing). Deliberately EMPTY until play reveals a need —\n"
    "# dialect rules are earned at the table, not invented speculatively.\n"
    "# Lines starting with # are stripped before injection.\n"
)


def _engine_overlay(engine_id: str) -> str:
    """The engine's dialect overlay, comment lines stripped. Empty by default."""
    p = _overlay_path(engine_id)
    if not p.exists():
        return ""
    lines = [l for l in p.read_text(encoding="utf-8").splitlines()
             if not l.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def _story_spine(story: dict, writer_engine_id: str) -> str:
    """The stable story layer (S7 cache-shaped stack order): base DM addendum →
    per-engine overlay → per-story instructions. Volatile content (brief,
    airtight note) is NOT here — it rides the end of the turn prompt."""
    stack = [_base_addendum()]
    overlay = _engine_overlay(writer_engine_id)
    if overlay:
        stack.append("## Engine notes (dialect for the current narrator model)\n\n" + overlay)
    ip = _instructions_path(story["slug"])
    instructions = ip.read_text(encoding="utf-8") if ip.exists() else ""
    if instructions.strip():
        stack.append("## This story\n\n" + instructions.strip())
    return "\n\n---\n\n".join(stack)


def _volatile_tail(story: dict, brief: str | None) -> str:
    """Per-turn volatile text: Director brief (always at the very end of the
    prompt, never mid-prompt) + the airtight note when armed."""
    parts = []
    if story.get("airtight"):
        parts.append(
            "## Airtight scene mode\n\nTreat ONLY the immediately preceding prose "
            "as your memory of the story so far. Do not reach back to reconstruct "
            "earlier scenes from memory; narrate this beat fresh from what is "
            "present now. Continuity you need has been provided above.")
    if brief:
        parts.append(
            "## Scene direction (observed detail to weave in naturally — do NOT "
            "explain or label it, do NOT reveal hidden meaning; some details are "
            "texture)\n\n" + brief)
    return "\n\n---\n\n".join(parts)


# ── S7: Kimi writer agent (default engine, thinking OFF) ─────────────────────
# RPG turns used to ride app.state.agent (thinking ON — Kimi's default). the user's
# S7 verdict: the default DM engine is Kimi K2.6 with thinking toggled OFF —
# narration needs the token budget in the prose, not the reasoning trace (same
# probe findings as the voice work: thinking off → temperature must be 0.6).
# Applied ONLY to the primary ChatKimi; backup untouched (failover correctness
# beats failover speed). Revert via RPG_DISABLE_THINKING=false in .env.
RPG_DISABLE_THINKING = os.environ.get("RPG_DISABLE_THINKING", "true").strip().lower() != "false"
_kimi_writer_agent = None
_kimi_writer_lock = threading.Lock()


def _get_kimi_writer_agent():
    """Dedicated RPG writer agent on the agent's existing Kimi stack (voice-agent
    pattern): same tools/prompt/checkpointer as the main agent, own LLM object
    so the thinking override never touches main chat. Lock: two concurrent
    first plays must not both build (and leak) an agent + SQLite connection."""
    global _kimi_writer_agent
    with _kimi_writer_lock:
        if _kimi_writer_agent is None:
            from langgraph.prebuilt import create_react_agent
            from .graph import (_get_llm_configs, _build_llm_for_config, LLMWithFallback,
                                ChatKimi, _build_core_memory_prompt, get_checkpointer,
                                CORE_MEMORY_TOOLS)

            def _detune(llm):
                if RPG_DISABLE_THINKING and type(llm) is ChatKimi:
                    llm.extra_body = {**(llm.extra_body or {}), "thinking": {"type": "disabled"}}
                    llm.temperature = 0.6
                    logger.info("rpg writer agent: Kimi thinking DISABLED (temperature 0.6)")
                return llm

            configs = _get_llm_configs()
            if not configs:
                raise RuntimeError("OPENAI_API_KEY is required (see .env)")
            if len(configs) > 1:
                llm = LLMWithFallback(configs)
                llm._llms = [_detune(inner) for inner in llm._llms]
            else:
                llm = _detune(_build_llm_for_config(*configs[0]))
            _kimi_writer_agent = create_react_agent(
                llm, tools=CORE_MEMORY_TOOLS, prompt=_build_core_memory_prompt,
                checkpointer=get_checkpointer())
    return _kimi_writer_agent


def _openrouter_writer_turn(story: dict, engine: dict, user_message: str,
                            spine: str, tail: str,
                            review=None, display_name: str | None = None) -> tuple[str, dict]:
    """S7: one DM turn on a non-Kimi engine via OpenRouter — the LEAN path.

    Deliberately NOT the full the agent agent: core memory, private blocks, daily
    summaries, and the tool manifest are the agent's personhood and NEVER route to
    a third party (S7.4 law). The engine gets the story and only the story:

        system  = spine (base addendum → engine overlay → story instructions)
        history = campaign turns (append-only — ideal cache prefix)
        user    = recalled memories + lore + timestamp + player input
                  + volatile tail (Director brief LAST, never mid-prompt)

    Mirrors chat()'s persistence contract: user+assistant to Postgres, retention
    to the story bank (fire-and-forget), goalie revision round (correction never
    persists). Returns (narration, usage)."""
    from datetime import datetime
    from .db import load_messages, append_messages
    from .graph import AGENT_TIMEZONE, RECENT_MESSAGES_LIMIT, LAST_ACTIVE_PATH, _format_current_time
    from .hindsight import recall_with_privacy_flag, retain_exchange
    from .rpg_engines import openrouter_chat

    slug = story["slug"]
    thread_id = f"rpg:{slug}"

    # Campaign history as proper role messages (tool rows are excluded at SQL
    # level; heartbeats never post here).
    rows = load_messages(thread_id, limit=RECENT_MESSAGES_LIMIT)
    history = [{"role": r["role"], "content": r.get("content") or ""}
               for r in rows if r.get("role") in ("user", "assistant")]

    # Story-scoped auto-recall + lore (same sources as the Kimi path).
    recall_text = ""
    try:
        recall_text, _ = recall_with_privacy_flag(
            RPG_BANK_ID, user_message.strip()[:300],
            tags=[thread_id], tags_match="all", exclude_private=False)
    except Exception:
        pass
    knowledge_text = ""
    try:
        from .knowledge_db import search as knowledge_db_search, is_configured as knowledge_is_configured
        if knowledge_is_configured():
            results = knowledge_db_search(query=user_message.strip()[:300],
                                          project=_projects_for_story(story), limit=4,
                                          access_level="admin")
            if results:
                parts = ["## Story lore (auto-retrieved)"]
                for r in results:
                    parts.append(f"[{r.get('doc_title', '')} | {r.get('section_header') or ''}]\n"
                                 f"{r.get('content', '')}")
                knowledge_text = "\n\n".join(parts)
    except Exception:
        pass

    now = datetime.now(AGENT_TIMEZONE)
    body = []
    if recall_text:
        body.append("## Recalled story memories (auto-injected)\n\n" + recall_text)
    if knowledge_text:
        body.append(knowledge_text)
    body.append(f"[{_format_current_time(now)}]\n\n{user_message}")
    if tail.strip():
        body.append(tail.strip())
    user_content = "\n\n---\n\n".join(body)

    messages = ([{"role": "system", "content": spine}] + history
                + [{"role": "user", "content": user_content}])

    narration, usage = openrouter_chat(engine, messages, max_tokens=6000)

    # Goalie revision round — one max; the correction directive is ephemeral.
    if review is not None and narration:
        try:
            correction = review(narration)
            if correction:
                messages2 = messages + [
                    {"role": "assistant", "content": narration},
                    {"role": "user", "content":
                        "[Director correction — revise your narration to comply. Do not "
                        "explain or acknowledge; reissue the corrected narration only.]\n"
                        + correction}]
                revised, usage2 = openrouter_chat(engine, messages2, max_tokens=6000)
                if revised:
                    narration = revised
                    usage = usage2
        except Exception:
            pass  # ship the original draft if revision fails

    # Persist + retain, mirroring chat(): clean user message + final narration
    # only — spine/brief/recall never enter history or the bank.
    _display = display_name or os.environ.get("USER_DISPLAY_NAME", "User")
    append_messages(thread_id, [("user", user_message, None, None),
                                ("assistant", narration, None, None)],
                    user_display_name=_display)
    # Only a real user turn marks the user as active (heartbeat skip window);
    # attributed automated callers don't.
    if display_name is None or display_name == os.environ.get("USER_DISPLAY_NAME", "User"):
        try:
            LAST_ACTIVE_PATH.write_text(str(time.time()))
        except Exception:
            pass
    act = story.get("current_act", 1)
    threading.Thread(
        target=retain_exchange,
        kwargs=dict(bank_id=RPG_BANK_ID, user_content=user_message,
                    assistant_content=narration, thread_id=thread_id,
                    user_id=os.environ.get("DEFAULT_USER_ID", "local:user"),
                    user_display_name=_display,
                    channel_type="rpg", is_group_chat=False,
                    extra_tags=[thread_id, f"act:{act}"]),
        daemon=True,
    ).start()
    return narration, usage


def _director_brief(story: dict, player_message: str, recent_prose: str) -> str | None:
    """Mystery mode only: ask the out-of-process Director for a scene brief
    (observables + act-posture, never truths). Returns None if the story isn't
    mystery, the Director is down, or no secrets are sealed yet. Briefs are
    NEVER persisted — the caller injects them into ephemeral_context only.
    Logged at DEBUG only (law: secrets/briefs never at info level)."""
    if not story.get("mystery"):
        return None
    try:
        r = httpx.post(f"{DIRECTOR_BASE}/brief", json={
            "slug": story["slug"],
            "act": story.get("current_act", 1),
            "player_message": player_message,
            "recent_prose": recent_prose[-4000:],
            "engine": story.get("director_engine") or "kimi",  # S7: resolved by the Director
        }, timeout=120)
        if r.status_code == 409:  # not sealed yet
            return None
        r.raise_for_status()
        brief = r.json().get("brief", "").strip()
        logger.debug("rpg: director brief for %s (act %s): %d chars",
                     story["slug"], story.get("current_act"), len(brief))
        return brief or None
    except Exception as e:
        logger.warning("rpg: director brief unavailable for %s: %s", story["slug"], e)
        return None


def _director_goalie(story: dict, draft: str) -> str | None:
    """Mystery + goalie on: Director checks the Writer's draft against hidden
    truth, returns correction directives (no reasons) or None if clear."""
    if not (story.get("mystery") and story.get("goalie")):
        return None
    try:
        r = httpx.post(f"{DIRECTOR_BASE}/goalie",
                       json={"slug": story["slug"], "draft": draft,
                             "engine": story.get("director_engine") or "kimi"},
                       timeout=120)
        if r.status_code == 409:
            return None
        r.raise_for_status()
        corr = r.json().get("correction", "").strip()
        return corr or None
    except Exception as e:
        logger.warning("rpg: goalie unavailable for %s: %s", story["slug"], e)
        return None


class AddendumReq(BaseModel):
    text: str


class EngineKeyReq(BaseModel):
    key: str


def register_rpg_routes(router: APIRouter) -> None:
    @router.get("/rpg/addendum")
    def get_addendum():
        """The effective base DM addendum + the built-in default (so the UI can
        offer 'revert'). is_custom = a UI-saved override is in effect."""
        p = _addendum_path()
        is_custom = p.exists() and bool(p.read_text(encoding="utf-8").strip())
        return {"text": _base_addendum(), "default": DEFAULT_DM_ADDENDUM,
                "is_custom": is_custom}

    @router.put("/rpg/addendum")
    def save_addendum(req: AddendumReq):
        """Save the edited base DM addendum (applies to all stories)."""
        _addendum_path().write_text(req.text, encoding="utf-8")
        return {"ok": True, "is_custom": bool(req.text.strip())}

    @router.post("/rpg/addendum/revert")
    def revert_addendum():
        """Revert to the built-in default draft (removes the saved override)."""
        p = _addendum_path()
        if p.exists():
            p.unlink()
        return {"ok": True, "text": DEFAULT_DM_ADDENDUM, "is_custom": False}

    # ── S7: engine selector ───────────────────────────────────────────────────

    @router.get("/rpg/engines")
    def list_engines():
        """The engine roster (config, not code — edit data/rpg_engines.json to
        add models) + whether the OpenRouter key is present. The key itself is
        NEVER returned."""
        from .rpg_engines import load_engines, engine_available, get_openrouter_key
        engines = []
        for e in load_engines():
            ok, why = engine_available(e)
            engines.append({**e, "available": ok, "unavailable_reason": why})
        return {"engines": engines, "openrouter_key_set": bool(get_openrouter_key()),
                "privacy_note": "Non-Kimi engines route story text through OpenRouter "
                                "(third party). Scoped to RPG threads only — the agent's "
                                "main chat and memories never route externally."}

    @router.post("/rpg/engines/key")
    def set_openrouter_key(req: "EngineKeyReq"):
        """Save the OpenRouter API key from the dashboard (written to .env,
        effective immediately — engine calls read it fresh per turn)."""
        key = req.key.strip()
        if not key:
            raise HTTPException(status_code=422, detail="Empty key")
        from .rpg_engines import save_openrouter_key
        save_openrouter_key(key)
        return {"ok": True, "openrouter_key_set": True}

    @router.get("/rpg/engines/{engine_id}/overlay")
    def get_overlay(engine_id: str):
        """Per-engine addendum overlay (the dialect layer) — raw file text plus
        the effective (comment-stripped) version that gets injected."""
        from .rpg_engines import get_engine
        engine = get_engine(engine_id)
        p = _overlay_path(engine["id"])
        if not p.exists():
            p.write_text(_OVERLAY_SEED.format(label=engine.get("label", engine["id"])),
                         encoding="utf-8")
        return {"engine": engine["id"], "text": p.read_text(encoding="utf-8"),
                "effective": _engine_overlay(engine["id"])}

    @router.put("/rpg/engines/{engine_id}/overlay")
    def put_overlay(engine_id: str, req: AddendumReq):
        from .rpg_engines import get_engine
        engine = get_engine(engine_id)
        _overlay_path(engine["id"]).write_text(req.text, encoding="utf-8")
        return {"ok": True, "effective": _engine_overlay(engine["id"])}

    @router.post("/rpg/engines/verify")
    def verify_engines_route():
        """S7.1 self-verification: each OpenRouter engine returns a completion;
        latency + cache-hit + per-turn cost (cached vs uncached) per engine."""
        from .rpg_engines import verify_engines
        return {"results": verify_engines()}

    # ── Common lore (shared across stories that opt in) ───────────────────────

    @router.get("/rpg/common-lore")
    def list_common_lore():
        """Docs in the shared rpg-common project — uploaded once, searched by
        every story with use_common_lore on."""
        try:
            return {"lore": _list_lore_project(COMMON_PROJECT), "configured": True}
        except Exception as e:
            logger.warning("rpg: common lore list failed: %s", e)
            return {"lore": [], "configured": False}

    @router.post("/rpg/common-lore")
    async def upload_common_lore(file: UploadFile = File(...)):
        data = await file.read()
        result = _ingest_lore_project(COMMON_PROJECT, data, file.filename or "lore.txt")
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Upload failed"))
        return result

    # ── Campaign export (browser download, .md or .txt) ───────────────────────

    @router.get("/rpg/stories/{slug}/export")
    def export_story(slug: str, fmt: str = "md"):
        """The full campaign transcript as a downloadable file. Only what was
        actually played is exported (user + assistant turns from Postgres);
        the DM spine, Director briefs, and recall blocks were never persisted,
        so the export is naturally clean prose."""
        from fastapi.responses import Response
        from .db import load_messages
        story = _story_row(slug)
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        if fmt not in ("md", "txt"):
            raise HTTPException(status_code=422, detail="fmt must be md or txt")
        rows = [r for r in load_messages(f"rpg:{slug}", limit=100000)
                if r.get("role") in ("user", "assistant")]
        player = os.environ.get("USER_DISPLAY_NAME", "Player")

        def turn_label(r):
            if r.get("role") == "assistant":
                return "the agent (DM)"
            return (r.get("metadata") or {}).get("role_display") or player

        def turn_date(r):
            return (r.get("metadata") or {}).get("date_est") or ""

        lines: list[str] = []
        if fmt == "md":
            lines.append(f"# {story['title']}")
            lines.append("")
            lines.append(f"*Campaign transcript — {len(rows)} turns, exported "
                         f"{time.strftime('%Y-%m-%d')}. Act {story.get('current_act', 1)}.*")
            last_date = None
            for r in rows:
                d = turn_date(r)
                if d and d != last_date:
                    lines.append("")
                    lines.append(f"## {d}")
                    last_date = d
                lines.append("")
                lines.append(f"**{turn_label(r)}:**")
                lines.append("")
                lines.append((r.get("content") or "").strip())
            body = "\n".join(lines).strip() + "\n"
            media = "text/markdown"
        else:
            lines.append(story["title"])
            lines.append("=" * len(story["title"]))
            lines.append("")
            for r in rows:
                lines.append(f"{turn_label(r)}:")
                lines.append((r.get("content") or "").strip())
                lines.append("")
            body = "\n".join(lines).strip() + "\n"
            media = "text/plain"
        fname = f"{slug}-campaign.{fmt}"
        return Response(content=body.encode("utf-8"), media_type=f"{media}; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @router.get("/rpg/stories")
    def list_stories():
        con = _db()
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM stories ORDER BY COALESCE(last_played, created_at) DESC")]
        con.close()
        # lore counts (best-effort — knowledge DB may be unconfigured)
        for s in rows:
            try:
                s["lore_count"] = len(_list_lore(s["slug"]))
            except Exception:
                s["lore_count"] = None
        return {"stories": rows}

    @router.post("/rpg/stories")
    def create_story(req: StoryCreate):
        title = req.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Title required")
        slug = _slugify(title)
        con = _db()
        if con.execute("SELECT 1 FROM stories WHERE slug=?", (slug,)).fetchone():
            slug = f"{slug}-{int(time.time()) % 10000}"
        with con:
            con.execute(
                "INSERT INTO stories (slug, title, mystery, goalie, use_common_lore, created_at) "
                "VALUES (?,?,?,1,?,?)",
                (slug, title, 1 if req.mystery else 0,
                 1 if req.use_common_lore else 0, time.time()))
        con.close()
        _story_dir(slug).mkdir(parents=True, exist_ok=True)
        _instructions_path(slug).write_text(
            f"# {title}\n\n(Story-specific instructions for the DM — tone, world "
            f"rules, canon, what to lean into. This stacks on top of the base DM "
            f"addendum.)\n", encoding="utf-8")
        logger.info("rpg: created story %r (slug=%s, mystery=%s)", title, slug, req.mystery)
        return {"slug": slug, "title": title, "thread_id": f"rpg:{slug}"}

    @router.get("/rpg/stories/{slug}")
    def get_story(slug: str):
        con = _db()
        s = con.execute("SELECT * FROM stories WHERE slug=?", (slug,)).fetchone()
        con.close()
        if not s:
            raise HTTPException(status_code=404, detail="Story not found")
        ip = _instructions_path(slug)
        return {**dict(s), "thread_id": f"rpg:{slug}",
                "instructions": ip.read_text(encoding="utf-8") if ip.exists() else ""}

    @router.patch("/rpg/stories/{slug}")
    def patch_story(slug: str, req: StoryPatch):
        sets, vals = [], []
        if req.title is not None:
            sets.append("title=?"); vals.append(req.title.strip())
        if req.mystery is not None:
            sets.append("mystery=?"); vals.append(1 if req.mystery else 0)
        if req.goalie is not None:
            sets.append("goalie=?"); vals.append(1 if req.goalie else 0)
        if req.airtight is not None:
            sets.append("airtight=?"); vals.append(1 if req.airtight else 0)
        if req.writer_engine is not None:
            from .rpg_engines import get_engine
            sets.append("writer_engine=?"); vals.append(get_engine(req.writer_engine)["id"])
        if req.director_engine is not None:
            from .rpg_engines import get_engine
            sets.append("director_engine=?"); vals.append(get_engine(req.director_engine)["id"])
        if req.use_common_lore is not None:
            sets.append("use_common_lore=?"); vals.append(1 if req.use_common_lore else 0)
        if sets:
            con = _db()
            with con:
                con.execute(f"UPDATE stories SET {', '.join(sets)} WHERE slug=?", (*vals, slug))
            con.close()
        return {"ok": True}

    @router.put("/rpg/stories/{slug}/instructions")
    def put_instructions(slug: str, req: InstructionsReq):
        d = _story_dir(slug)
        if not d.exists():
            raise HTTPException(status_code=404, detail="Story not found")
        _instructions_path(slug).write_text(req.instructions, encoding="utf-8")
        return {"ok": True}

    @router.get("/rpg/stories/{slug}/lore")
    def list_lore(slug: str):
        try:
            return {"lore": _list_lore(slug), "configured": True}
        except Exception as e:
            logger.warning("rpg: lore list failed for %s: %s", slug, e)
            return {"lore": [], "configured": False}

    @router.post("/rpg/stories/{slug}/lore")
    async def upload_lore(slug: str, file: UploadFile = File(...)):
        con = _db()
        exists = con.execute("SELECT 1 FROM stories WHERE slug=?", (slug,)).fetchone()
        con.close()
        if not exists:
            raise HTTPException(status_code=404, detail="Story not found")
        data = await file.read()
        result = _ingest_lore(slug, data, file.filename or "lore.txt")
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Upload failed"))
        return result

    @router.post("/rpg/stories/{slug}/seal")
    def seal_story(slug: str, req: "SealReq"):
        """Arm the mystery: the out-of-process Director authors + encrypts the
        secrets doc and returns its hash (stored on the card). the agent's process
        never sees the doc — only the hash comes back here."""
        story = _story_row(slug)
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        # The story instructions are the author's established canon — the Director
        # authors the hidden truth BENEATH them, never a rival story (the user hit
        # exactly this: sealed secrets invented a different protagonist).
        ip = _instructions_path(slug)
        instructions = (ip.read_text(encoding="utf-8") if ip.exists() else "").strip()
        try:
            r = httpx.post(f"{DIRECTOR_BASE}/seal", json={
                "slug": slug, "lore": _lore_fulltext(story), "seed": req.seed,
                "instructions": instructions[:8000],
                "engine": story.get("director_engine") or "kimi"}, timeout=300)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Director unavailable: {e}")
        con = _db()
        with con:
            con.execute("UPDATE stories SET mystery=1, sealed_hash=? WHERE slug=?",
                        (data["hash"], slug))
        con.close()
        return {"sealed": True, "hash": data["hash"]}

    @router.get("/rpg/stories/{slug}/seal")
    def seal_status(slug: str):
        try:
            r = httpx.get(f"{DIRECTOR_BASE}/seal/{slug}", timeout=30)
            return r.json()
        except Exception:
            return {"sealed": False, "director": "unavailable"}

    @router.post("/rpg/stories/{slug}/reveal")
    def reveal_secrets(slug: str):
        """S4 break-the-seal: fetch the decrypted secrets from the Director so
        the human can verify the mystery was fixed from turn one."""
        try:
            r = httpx.post(f"{DIRECTOR_BASE}/reveal/{slug}", timeout=30)
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail="Nothing sealed for this story")
            return r.json()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Director unavailable: {e}")

    @router.get("/rpg/stories/{slug}/messages")
    def story_messages(slug: str, limit: int = 200):
        from .db import load_messages
        rows = load_messages(f"rpg:{slug}", limit=limit)
        return {"messages": [
            {"role": r.get("role"), "content": r.get("content"), "metadata": r.get("metadata")}
            for r in rows if r.get("role") != "tool"]}

    @router.post("/rpg/stories/{slug}/act")
    def set_act(slug: str, act: int):
        con = _db()
        with con:
            con.execute("UPDATE stories SET current_act=? WHERE slug=?", (max(1, act), slug))
        con.close()
        return {"ok": True, "current_act": max(1, act)}

    @router.post("/rpg/stories/{slug}/play")
    def play_turn(slug: str, req: PlayReq, request: Request):
        story = _story_row(slug)
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        from .graph import chat
        from .db import load_messages

        # Scene state for the Director: the recent player-visible prose only.
        recent = load_messages(f"rpg:{slug}", limit=8)
        recent_prose = "\n\n".join(
            m.get("content", "") for m in recent if m.get("role") == "assistant")

        # S7 cache-shaped stack: STABLE spine (base addendum → per-engine overlay
        # → per-story instructions) separated from the VOLATILE tail (Director
        # brief + airtight note — always at the very end of the prompt, never
        # mid-prompt). Both prompt-only, never persisted.
        from .rpg_engines import get_engine, engine_available, estimate_tokens, turn_cost
        engine = get_engine(story.get("writer_engine"))
        ok, why = engine_available(engine)
        engine_note = None
        if not ok:
            engine_note = f"engine {engine['id']} unavailable ({why}) — fell back to Kimi"
            logger.warning("rpg: %s", engine_note)
            engine = get_engine("kimi")

        spine = _story_spine(story, engine["id"])
        brief = _director_brief(story, req.message, recent_prose)
        tail = _volatile_tail(story, brief)

        review = (lambda draft: _director_goalie(story, draft)) if story.get("goalie") else None
        act = story.get("current_act", 1)
        display_name = req.caller or os.environ.get("USER_DISPLAY_NAME", "User")
        usage: dict = {}
        try:
            if engine.get("model"):
                # Non-Kimi engine → LEAN OpenRouter path (story-scoped; the agent's
                # personhood — core memory, tools — never routes externally).
                narration, usage = _openrouter_writer_turn(
                    story, engine, req.message, spine, tail, review=review,
                    display_name=display_name)
                result = {"last_ai_content": narration}
            else:
                # Kimi (default) → existing full-agent path, thinking OFF, on a
                # dedicated writer agent so main chat is untouched.
                result = chat(
                    _get_kimi_writer_agent(),
                    f"rpg:{slug}",
                    req.message,
                    user_display_name=display_name,
                    channel_type="rpg",
                    channel_mode="admin",
                    system_spine=spine,
                    ephemeral_tail=tail,
                    use_knowledge=True,
                    knowledge_project=_projects_for_story(story),
                    story_bank_id=RPG_BANK_ID,
                    story_tag=f"rpg:{slug}",
                    story_extra_tags=[f"act:{act}"],
                    story_review=review,
                    mark_user_active=(req.caller is None),
                )
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            logger.error("rpg play error (%s): %s", slug, e)
            raise HTTPException(status_code=500, detail=f"DM error: {e}")

        # Context-window + price-line warnings (S7.3 / Grok's 200k step).
        warnings: list[str] = []
        if engine_note:
            warnings.append(engine_note)
        try:
            est = (usage.get("prompt_tokens")
                   or estimate_tokens(spine) + estimate_tokens(recent_prose) + 4000)
            window = engine.get("context_window") or 0
            if window and est > 0.8 * window:
                warnings.append(
                    f"campaign context ≈{est:,} tokens — approaching {engine['label']}'s "
                    f"{window:,}-token window; consider airtight mode or a recap")
            step = engine.get("price_step")
            if step and est > step.get("tokens", 10**9):
                warnings.append(
                    f"turn crossed {engine['label']}'s {step['tokens']:,}-token pricing "
                    f"line — input/output now billed ~{step.get('multiplier', 2)}x")
        except Exception:
            pass

        con = _db()
        with con:
            con.execute("UPDATE stories SET last_played=? WHERE slug=?", (time.time(), slug))
        con.close()
        # now-playing awareness for main the agent (S5) — best-effort.
        try:
            import json as _json
            np = Path(__file__).resolve().parents[2] / "data" / "now_playing.json"
            np.write_text(_json.dumps({"slug": slug, "title": story["title"],
                                       "act": act, "updated_at": time.time()}), encoding="utf-8")
        except OSError:
            pass
        resp: dict = {"response": result.get("last_ai_content", ""), "engine": engine["id"]}
        if warnings:
            resp["warnings"] = warnings
        if usage:
            from .rpg_engines import cached_tokens_from_usage
            resp["usage"] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cached_tokens": cached_tokens_from_usage(usage),
                "cost": usage.get("cost"),
            }
        return resp

    @router.delete("/rpg/stories/{slug}")
    def delete_story(slug: str):
        import shutil
        con = _db()
        with con:
            con.execute("DELETE FROM stories WHERE slug=?", (slug,))
        con.close()
        d = _story_dir(slug)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        # Note: lore docs in the knowledge bank are left (shared store); a future
        # cleanup could remove by tag. Thread history (S2) is separate too.
        return {"ok": True}
