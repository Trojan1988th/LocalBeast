r"""The Director (RPG S3) — a quarantined, out-of-process secret-keeper.

WHY OUT OF PROCESS (proven, not assumed): the agent's python_repl inherits his
process env AND can read arbitrary files (S0/S1 probes). So the story's
secrets and the encryption key must live in a DIFFERENT process whose env
the agent never loads. This service holds them; the agent's process calls it over HTTP
and receives ONLY briefs (observable stage directions) — never truths.

The six design laws (RPG_BUILD.md), enforced structurally here:
  1. Briefs never persist — the RPG route injects them ephemerally; this
     service never writes them anywhere.
  2. The Director is route-level plumbing, not a the agent tool.
  3. Briefs are observable behavior, never labeled interpretation.
  4. Briefs MUST carry decoy/mask-fitting noise (in the system prompt below).
  5. Quarantine — this service has NO tools, NO the agent memory, NO Hindsight,
     NO access to anything but its own encrypted secrets store.
  6. Secrets never enter the agent's process — enforced by encryption at rest +
     this being a separate process with its own key.

Storage: director/secrets/<slug>.enc (Fernet-encrypted). Key: director/
.director_key (generated locally on first run, gitignored, NEVER committed,
and NOT present in the agent's process env). Logs: DEBUG only for anything that
could echo a secret or a brief.

Run: python director/director_service.py   (port 5008; start_director.cmd)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

_HERE = Path(__file__).resolve().parent
# Provider creds come from the main .env (not secret — the agent has them too).
# override=True so the .env file wins over any stale value in the shell env.
load_dotenv(_HERE.parent / ".env", override=True)
# The Director's own .env may override provider/model + tuning (optional).
load_dotenv(_HERE / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] director: %(message)s")
logger = logging.getLogger("director")
# Nothing secret or brief-shaped ever logs above DEBUG (law). DEBUG is opt-in.
logging.getLogger("httpx").setLevel(logging.WARNING)

SECRETS_DIR = _HERE / "secrets"
SECRETS_DIR.mkdir(exist_ok=True)
KEY_FILE = _HERE / ".director_key"
PORT = int(os.getenv("DIRECTOR_PORT", "5008"))

# LLM (self-contained — NO import of graph.py; that is the quarantine).
LLM_KEY = os.getenv("DIRECTOR_LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
LLM_BASE = (os.getenv("DIRECTOR_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL", "")).rstrip("/")
LLM_MODEL = os.getenv("DIRECTOR_LLM_MODEL") or os.getenv("OPENAI_MODEL_NAME", "")


def _get_key() -> bytes:
    """Load the Fernet key from env or the local key file; generate on first
    run. The key is NEVER committed and NEVER placed in the agent's process env."""
    env_key = os.getenv("DIRECTOR_KEY")
    if env_key:
        return env_key.encode()
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    logger.info("generated a new Director key at %s (keep it local; never commit)", KEY_FILE.name)
    return key


_fernet = Fernet(_get_key())


def _store_path(slug: str) -> Path:
    safe = re.sub(r"[^a-z0-9-]", "", slug.lower())[:64]
    return SECRETS_DIR / f"{safe}.enc"


def _load_store(slug: str) -> dict | None:
    p = _store_path(slug)
    if not p.exists():
        return None
    return json.loads(_fernet.decrypt(p.read_bytes()).decode())


def _save_store(slug: str, store: dict) -> None:
    _store_path(slug).write_bytes(_fernet.encrypt(json.dumps(store).encode()))


# Kimi-for-coding quirk (from the voice work): with thinking ENABLED it emits a
# long reasoning_content that eats the token budget and can leave content empty,
# and it requires temperature==1. With thinking DISABLED, content gets the full
# budget and temperature must be 0.6. We disable thinking on the Kimi endpoint —
# cleaner, no reasoning to strip, all budget to the doc. Portable elsewhere.
_IS_KIMI = "kimi.com" in LLM_BASE
_TEMP = os.getenv("DIRECTOR_TEMPERATURE")


def _resolve_engine(engine_id: str | None) -> dict | None:
    """S7: resolve a director_engine id via the shared roster module.
    src.agent.rpg_engines is PURE (config + httpx, no graph/db imports), so
    importing it does not breach the quarantine — the law is 'no graph import'.
    Returns None for the default (kimi / unknown / roster unavailable) —
    meaning: use the Director's own env-configured LLM exactly as before."""
    if not engine_id or engine_id == "kimi":
        return None
    try:
        import sys
        root = str(_HERE.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from src.agent.rpg_engines import get_engine, get_openrouter_key
        engine = get_engine(engine_id)
        if not engine.get("model") or not get_openrouter_key():
            logger.warning("engine %s unavailable in Director — using default LLM", engine_id)
            return None
        return engine
    except Exception as e:
        logger.warning("engine resolution failed (%s) — using default LLM", e)
        return None


def _llm(system: str, user: str, *, temperature: float | None = None,
         max_tokens: int = 2000, engine: dict | None = None) -> str:
    if engine is not None:
        # S7: non-default Director engine → OpenRouter, still 100% server-side in
        # THIS process. Secrets in the prompt go to the chosen provider (same
        # trust boundary as the Kimi default — an LLM must read the secrets to
        # direct); they still never touch the agent's process, history, or logs.
        from src.agent.rpg_engines import openrouter_chat
        try:
            text, _usage = openrouter_chat(engine, [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], max_tokens=max_tokens, temperature=temperature)
            return text
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
    if not (LLM_KEY and LLM_BASE and LLM_MODEL):
        raise HTTPException(status_code=503, detail="Director LLM not configured (OPENAI_* env)")
    payload = {"model": LLM_MODEL, "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if _IS_KIMI:
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = 0.6
    elif _TEMP is not None:
        payload["temperature"] = float(_TEMP)
    last_err = None
    for attempt in range(3):  # providers occasionally drop the connection mid-call
        try:
            with httpx.Client(timeout=180) as c:
                r = c.post(f"{LLM_BASE}/chat/completions",
                           headers={"Authorization": f"Bearer {LLM_KEY}"}, json=payload)
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                out = (msg.get("content") or "").strip()
                return out or (msg.get("reasoning_content") or "").strip()
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise HTTPException(status_code=503, detail=f"Director LLM unreachable after retries: {last_err}")


# ── System prompts (the Director's soul) ─────────────────────────────────────
SEAL_SYSTEM = (
    "You are the Director: a secret-keeping story architect for a blind-play mystery "
    "RPG. From the provided canon, lore, and the human's seed, author the COMPLETE "
    "HIDDEN TRUTH of this story as a sealed design document. Include: (1) what is really "
    "going on beneath the surface; (2) each significant NPC's SOUL (their true nature "
    "and secret) versus their MASK (the face they present); (3) a three-act arc with "
    "what each act is FOR; (4) explicit, concrete REVEAL CONDITIONS keyed to acts and "
    "player actions — when each hidden truth may surface. Be specific and usable.\n"
    "CANON IS BINDING: if STORY INSTRUCTIONS are provided, they are the author's "
    "established story — the protagonist (her name, backstory, situation), the named "
    "characters, the world, and the tone are FIXED. You are adding the secret layer "
    "BENEATH that story, not writing a rival one. Never rename or replace the "
    "protagonist, never contradict the canon; every secret must be consistent with it "
    "and, when revealed, must recast what the player already knows in a deeper light. "
    "The seed states what the mystery is about; weave it INTO the established story.\n"
    "This document is SEALED: the player never sees it, and the Writer (the DM) never "
    "sees it. Write it for your own eyes only. Output the document as prose with clear "
    "headers (## Hidden Truth, ## NPCs: Soul vs Mask, ## Three-Act Arc, ## Reveal "
    "Conditions)."
)

BRIEF_SYSTEM = (
    "You are the Director of a blind-play mystery. You hold the sealed secrets. The "
    "Writer (the DM) will narrate the next beat but must NEVER learn hidden truth. "
    "Given the secrets, the current act, the recent prose, and the player's action, "
    "issue a SCENE BRIEF for the Writer. LAWS:\n"
    "- OBSERVABLE BEHAVIOR ONLY. Physical, sensory, external detail an onlooker could "
    "witness: 'her hand trembles as she sets down the cup; she does not finish the "
    "sentence.' NEVER interpretation, NEVER 'because', NEVER the reason, NEVER the "
    "truth. If you catch yourself explaining, delete it.\n"
    "- DECOY NOISE IS MANDATORY. Always include one or two mask-fitting decoy details "
    "that mean nothing, so neither the Writer nor the player can tell which details "
    "are load-bearing. Do not mark which is which.\n"
    "- ACT POSTURE. Open the brief with a one-line spoiler-free pacing posture for the "
    "current act (e.g. 'Act 1 — home ring: establish texture and trust, no reveals "
    "this act'). You own the arc; keep the Writer in the right gear without telling "
    "them why.\n"
    "- REVEALS ARE YOURS ALONE. Only if a reveal condition for THIS act has genuinely "
    "triggered may you fold the now-unlocked truth into the brief as a narrative "
    "instruction. Otherwise, withhold.\n"
    "Output 3-7 short lines: the act-posture line, then observable directives + decoys. "
    "No preamble, no labels like 'DECOY' or 'MEANINGFUL'."
)

GOALIE_SYSTEM = (
    "You are the Director acting as goalie. You hold the sealed secrets. The Writer has "
    "produced a narration draft. Check it ONLY for contradictions of hidden truth or "
    "accidental spoilers (a detail that gives away a soul/mask, a reveal firing before "
    "its condition, an NPC acting against their secret nature). If the draft is clean, "
    "reply with exactly: CLEAR. If not, reply with terse CORRECTION DIRECTIVES telling "
    "the Writer what to change — WITHOUT stating the reason or the hidden truth (e.g. "
    "'The steward should not meet her eyes when the ring is mentioned; have him look "
    "away.'). Never explain why. Never reveal a secret in the correction."
)

app = FastAPI(title="Director")


class SealReq(BaseModel):
    slug: str
    lore: str = ""
    seed: str = ""
    instructions: str = ""  # the author's story instructions — BINDING canon
    engine: str = "kimi"  # S7: director_engine — resolved via the shared roster


class BriefReq(BaseModel):
    slug: str
    act: int = 1
    player_message: str = ""
    recent_prose: str = ""
    engine: str = "kimi"  # S7


class GoalieReq(BaseModel):
    slug: str
    draft: str = ""
    engine: str = "kimi"  # S7


@app.get("/health")
def health():
    return {"status": "ok", "llm_configured": bool(LLM_KEY and LLM_BASE and LLM_MODEL)}


@app.post("/seal")
def seal(req: SealReq):
    """Author + encrypt the secrets doc; compute + store its SHA-256. Re-sealing
    keeps hash history (S4 immutability)."""
    user = (f"STORY INSTRUCTIONS (the author's established canon — BINDING; the "
            f"protagonist, characters, world and tone here are fixed):\n"
            f"{req.instructions[:8000]}\n\n" if req.instructions.strip() else "") + \
           (f"LORE:\n{req.lore[:12000]}\n\n" if req.lore else "") + \
           (f"SEED (the human's mystery premise — weave it INTO the canon above):\n{req.seed}"
            if req.seed else
            "SEED: (none given — invent a compact, fair mystery beneath the canon and lore.)")
    doc = _llm(SEAL_SYSTEM, user, temperature=0.8, max_tokens=3000,
               engine=_resolve_engine(req.engine))
    digest = hashlib.sha256(doc.encode("utf-8")).hexdigest()
    store = _load_store(req.slug) or {"history": []}
    if store.get("current"):
        store["history"].append({"hash": store["current"]["hash"],
                                 "sealed_at": store["current"]["sealed_at"]})
    store["current"] = {"doc": doc, "hash": digest, "sealed_at": time.time()}
    _save_store(req.slug, store)
    logger.info("sealed %s (sha256 %s…, doc %d chars)", req.slug, digest[:12], len(doc))
    return {"sealed": True, "hash": digest, "sealed_at": store["current"]["sealed_at"]}


@app.get("/seal/{slug}")
def seal_status(slug: str):
    store = _load_store(slug)
    if not store or not store.get("current"):
        return {"sealed": False}
    return {"sealed": True, "hash": store["current"]["hash"],
            "sealed_at": store["current"]["sealed_at"], "versions": len(store.get("history", [])) + 1}


def _scene_relevant(doc: str, brief_ctx: str, threshold_chars: int = 6000) -> str:
    """S4 variable secret loading: for large docs, keep only sections whose
    header or body mentions a token present in the current scene. Small docs
    pass through whole."""
    if len(doc) <= threshold_chars:
        return doc
    ctx_tokens = set(re.findall(r"[A-Za-z]{4,}", brief_ctx.lower()))
    sections = re.split(r"(?=^##\s)", doc, flags=re.M)
    kept = [s for s in sections
            if not s.startswith("##")  # preamble / arc always kept
            or any(tok in s.lower() for tok in ctx_tokens)
            or s.lower().startswith(("## three-act", "## reveal"))]
    out = "".join(kept)
    return out if len(out) > 500 else doc  # never starve the Director


@app.post("/brief")
def brief(req: BriefReq):
    store = _load_store(req.slug)
    if not store or not store.get("current"):
        raise HTTPException(status_code=409, detail="Story not sealed")
    ctx = f"{req.player_message}\n{req.recent_prose}"
    doc = _scene_relevant(store["current"]["doc"], ctx)
    user = (f"CURRENT ACT: {req.act}\n\nSECRETS (sealed — for your eyes only):\n{doc}\n\n"
            f"RECENT PROSE (what the player has seen):\n{req.recent_prose[-3500:]}\n\n"
            f"PLAYER'S ACTION:\n{req.player_message}\n\n"
            "Issue the scene brief now (act-posture line first).")
    out = _llm(BRIEF_SYSTEM, user, temperature=0.85, max_tokens=600,
               engine=_resolve_engine(req.engine))
    logger.debug("brief %s act%s -> %d chars", req.slug, req.act, len(out))  # DEBUG only
    return {"brief": out}


@app.post("/goalie")
def goalie(req: GoalieReq):
    store = _load_store(req.slug)
    if not store or not store.get("current"):
        raise HTTPException(status_code=409, detail="Story not sealed")
    user = (f"SECRETS (sealed):\n{store['current']['doc']}\n\n"
            f"WRITER'S DRAFT:\n{req.draft}\n\nCheck it. CLEAR or correction directives only.")
    out = _llm(GOALIE_SYSTEM, user, temperature=0.3, max_tokens=400,
               engine=_resolve_engine(req.engine))
    correction = "" if out.strip().upper().startswith("CLEAR") else out.strip()
    logger.debug("goalie %s -> %s", req.slug, "clear" if not correction else f"{len(correction)} chars")
    return {"correction": correction}


@app.post("/reveal/{slug}")
def reveal(slug: str):
    """S4 break-the-seal: return the decrypted secrets doc + hash so the human
    can verify the mystery was fixed from turn one. Intended for post-finale."""
    store = _load_store(slug)
    if not store or not store.get("current"):
        raise HTTPException(status_code=404, detail="Nothing sealed for this story")
    cur = store["current"]
    return {"doc": cur["doc"], "hash": cur["hash"], "sealed_at": cur["sealed_at"],
            "history": store.get("history", [])}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
