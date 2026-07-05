r"""RPG (S7): DM engine roster + OpenRouter client.

Separates the DM's IDENTITY (base addendum, per-story instructions, story-dm
bank, knowledge project — all model-agnostic) from the DM's ENGINE (the LLM
that performs the narration). A per-story setting routes RPG turns through a
chosen engine via OpenRouter; the default is Kimi (the agent's normal stack), so
nothing changes unless an engine is picked.

STANDALONE ON PURPOSE: this module imports nothing from graph/db/hindsight.
The out-of-process Director imports it too (for director_engine resolution),
and the quarantine law is "no graph import" — pure config + httpx only.

Roster lives in data/rpg_engines.json (seeded below on first load; edit the
file to add/remove engines — config, not code). Read fresh on every call so
changes take effect without a restart (discord_config pattern).

Caching (S7 requirement): every engine in the roster records its caching model.
As of 2026-07 all rostered providers cache AUTOMATICALLY via OpenRouter — no
Anthropic-style cache_control breakpoints are needed (those exist only for
Anthropic/Qwen/Gemini-explicit). The request SHAPE still matters: stable prefix
first (addendum → engine overlay → story instructions → campaign history),
volatile content last (player input; mystery brief at the very end). Cache hits
are asserted via usage.prompt_tokens_details.cached_tokens in the response.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger("agent.rpg_engines")

_ROOT = Path(__file__).resolve().parents[2]
ENGINES_PATH = _ROOT / "data" / "rpg_engines.json"
ENV_PATH = _ROOT / ".env"

OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Seed roster (written to data/rpg_engines.json on first load; the file is the
# source of truth after that). Model slugs verified live on OpenRouter 2026-07-04.
# NOTE: the S7 spec named Grok 4.1 and Gemini 3 Pro — both retired upstream
# (xAI May 2026, Google Mar 2026); grok-4.3 / gemini-3.1-pro are the live heirs.
#
# Field notes:
#   model            — OpenRouter slug; null = the agent's existing stack (not OpenRouter)
#   caching          — "automatic" | "explicit" | "none" (all current entries automatic)
#   cache_read_mult  — cached-input price as a fraction of input price (for cost calc)
#   price_step       — {"tokens": N, "multiplier": M}: input/output prices multiply by
#                      M when the request exceeds N total tokens (Grok's 200k line —
#                      the UI warns when a campaign approaches it)
#   provider         — OpenRouter provider-routing prefs. fp8 preferred over fp4 for
#                      prose engines (quantization degrades creative prose subtly but
#                      really); allow_fallbacks=false would strand us, so we keep it
#                      true — fp8 first, degrade gracefully.
#   reasoning        — OpenRouter reasoning param sent with each request (per-engine).
#                      Gemini 3.1 CANNOT disable reasoning (mandatory) — omit.
_DEFAULT_ENGINES: dict = {
    "_comment": "RPG DM engine roster. Edit to add/remove engines (config, not code). "
                "id 'kimi' is the agent's existing stack and must stay first/default.",
    "engines": [
        {
            "id": "kimi",
            "label": "Kimi (the agent's default)",
            "model": None,
            "description": "The incumbent — slow-burn romance champion, low slop. "
                           "Runs on the agent's existing stack (thinking disabled for RPG turns).",
            "context_window": 262144,
            "good_at_rules": True,
            "caching": "automatic",
            "cache_read_mult": 0.25,
            "price_in": 0.66, "price_out": 3.41,
        },
        {
            "id": "grok-4.3",
            "label": "Grok 4.3 (xAI)",
            "model": "x-ai/grok-4.3",
            "description": "Top creative-writing Elo lineage — vivid voice, least preachy. "
                           "Weaker at rules/state adjudication.",
            "context_window": 1000000,
            "good_at_rules": False,
            "caching": "automatic",
            "cache_read_mult": 0.16,
            "price_in": 1.25, "price_out": 2.50,
            "price_step": {"tokens": 200000, "multiplier": 2,
                           "note": "xAI bills ~2x above 200k tokens per request"},
            "reasoning": {"effort": "low"},
        },
        {
            "id": "glm-5.2",
            "label": "GLM-5.2 (Z.ai)",
            "model": "z-ai/glm-5.2",
            "description": "Best all-around open DM — permissive, consistent long voice, cheap.",
            "context_window": 1048576,
            "good_at_rules": True,
            "caching": "automatic",
            "cache_read_mult": 0.19,
            "price_in": 0.77, "price_out": 2.42,
            "provider": {"quantizations": ["fp8"], "allow_fallbacks": True},
        },
        {
            "id": "gemini-3.1-pro",
            "label": "Gemini 3.1 Pro (Google)",
            "model": "google/gemini-3.1-pro-preview",
            "description": "Restrained, controllable prose; huge context; strong plot-driving. "
                           "The upgrade path from 2.5 Pro. Reasoning cannot be disabled.",
            "context_window": 1048576,
            "good_at_rules": True,
            "caching": "automatic",
            "cache_read_mult": 0.10,
            "price_in": 2.00, "price_out": 12.00,
        },
        {
            "id": "deepseek-v3.2",
            "label": "DeepSeek V3.2",
            "model": "deepseek/deepseek-v3.2",
            "description": "Realism, anti-positivity-bias, aggression. Pinned V3.2 — "
                           "NOT V4 (mid-2026 regression reputation).",
            "context_window": 131072,
            "good_at_rules": True,
            "caching": "automatic",
            "cache_read_mult": 0.10,
            "price_in": 0.2288, "price_out": 0.3432,
            "provider": {"quantizations": ["fp8"], "allow_fallbacks": True},
        },
    ],
}


def load_engines() -> list[dict]:
    """Roster, fresh from disk each call (config changes need no restart).
    Seeds the default roster on first run."""
    if not ENGINES_PATH.exists():
        ENGINES_PATH.parent.mkdir(parents=True, exist_ok=True)
        ENGINES_PATH.write_text(json.dumps(_DEFAULT_ENGINES, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        logger.info("rpg_engines: seeded default roster at %s", ENGINES_PATH)
    try:
        data = json.loads(ENGINES_PATH.read_text(encoding="utf-8"))
        engines = data.get("engines", [])
        return engines if engines else list(_DEFAULT_ENGINES["engines"])
    except Exception as e:
        logger.warning("rpg_engines: roster unreadable (%s); using built-in defaults", e)
        return list(_DEFAULT_ENGINES["engines"])


def get_engine(engine_id: str | None) -> dict:
    """Resolve an engine id; unknown/missing ids fall back to the default (kimi)."""
    engines = load_engines()
    for e in engines:
        if e.get("id") == (engine_id or "kimi"):
            return e
    return engines[0]


def engine_available(engine: dict) -> tuple[bool, str]:
    """Is this engine usable right now? Kimi is always available (existing stack);
    OpenRouter engines need a model slug + key present."""
    if not engine.get("model"):
        return True, ""
    if not engine.get("model", "").strip():
        return False, "no model slug configured"
    if not get_openrouter_key():
        return False, "OPENROUTER_API_KEY not set"
    return True, ""


def get_openrouter_key() -> str:
    """OpenRouter key, call-time fresh: process env first, then .env re-read —
    so a key saved from the dashboard works immediately, in this process AND in
    the Director (separate process, same .env)."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def save_openrouter_key(key: str) -> None:
    """Persist the key to .env (update-or-append) + the live process env.
    .env is gitignored; the key never enters the roster file or any log."""
    key = key.strip()
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("OPENROUTER_API_KEY="):
            lines[i] = f"OPENROUTER_API_KEY={key}"
            replaced = True
            break
    if not replaced:
        lines.append(f"OPENROUTER_API_KEY={key}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["OPENROUTER_API_KEY"] = key
    logger.info("rpg_engines: OpenRouter key saved (len %d)", len(key))


def estimate_tokens(text: str) -> int:
    """Cheap chars/4 estimate — good enough for context-window warnings."""
    return len(text) // 4


def turn_cost(engine: dict, tokens_in: int, tokens_out: int,
              cached_tokens: int = 0) -> float:
    """Estimated $ for one turn on this engine, honoring cache-read pricing and
    the price step (Grok's 200k line)."""
    p_in, p_out = engine.get("price_in", 0.0), engine.get("price_out", 0.0)
    step = engine.get("price_step")
    if step and tokens_in + tokens_out > step.get("tokens", 10**9):
        p_in *= step.get("multiplier", 1)
        p_out *= step.get("multiplier", 1)
    fresh_in = max(0, tokens_in - cached_tokens)
    cached_cost = cached_tokens * p_in * engine.get("cache_read_mult", 1.0)
    return (fresh_in * p_in + cached_cost + tokens_out * p_out) / 1_000_000


def openrouter_chat(engine: dict, messages: list[dict], *,
                    max_tokens: int = 4000, temperature: float | None = None,
                    timeout: float = 300.0) -> tuple[str, dict]:
    """One OpenRouter chat completion. Returns (text, usage).

    usage is OpenRouter's usage object (always present in responses as of 2026):
    prompt_tokens, completion_tokens, prompt_tokens_details.cached_tokens, cost.
    Retries transient connection drops (same pattern as the Director's _llm)."""
    key = get_openrouter_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set (add it in the RPG tab or .env)")
    model = engine.get("model")
    if not model:
        raise RuntimeError(f"engine {engine.get('id')} has no OpenRouter model slug")

    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    if engine.get("provider"):
        payload["provider"] = engine["provider"]
    if engine.get("reasoning"):
        payload["reasoning"] = engine["reasoning"]

    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "http://localhost:8000",
        "X-Title": "Agent RPG DM",
    }
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(f"{OPENROUTER_BASE}/chat/completions", headers=headers, json=payload)
                r.raise_for_status()
                data = r.json()
                msg = data["choices"][0]["message"]
                text = (msg.get("content") or "").strip()
                if not text:  # reasoning ate the budget — same fallback as ChatKimi
                    text = (msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
                return text, data.get("usage", {}) or {}
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ReadTimeout) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
        except httpx.HTTPStatusError as e:
            # 4xx won't improve on retry; surface the provider's message
            detail = ""
            try:
                detail = e.response.json().get("error", {}).get("message", "")
            except Exception:
                pass
            raise RuntimeError(
                f"OpenRouter {e.response.status_code} for {model}: {detail or e}") from e
    raise RuntimeError(f"OpenRouter unreachable after retries: {last_err}")


def cached_tokens_from_usage(usage: dict) -> int:
    return int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)


def verify_engines() -> list[dict]:
    """S7.1 self-verification: for every OpenRouter engine, make two calls with an
    identical ~stable prefix and assert the second registers a cache hit
    (usage.prompt_tokens_details.cached_tokens > 0). Reports latency + cost per
    engine, cached vs uncached. Kimi (default stack) is reported by the caller —
    it doesn't route through here."""
    # A stable prefix long enough to be cacheable everywhere (most providers
    # need >=1024 cached tokens; DeepSeek caches in 64-token blocks).
    spine = ("You are a tabletop RPG narrator. House rules follow.\n\n"
             + ("The narration is grounded, sensory, and specific. The world acts on "
                "its own timers. End every response facing something, not having done "
                "something. Never write the player's dialogue or decisions. ") * 220)
    results = []
    for engine in load_engines():
        if not engine.get("model"):
            continue  # kimi — existing stack, verified separately
        ok, why = engine_available(engine)
        if not ok:
            results.append({"id": engine["id"], "ok": False, "error": why})
            continue
        row: dict = {"id": engine["id"], "model": engine["model"], "ok": True}
        try:
            messages = [{"role": "system", "content": spine},
                        {"role": "user", "content": "In one short sentence: the tavern door opens. What enters?"}]
            t0 = time.time()
            text1, u1 = openrouter_chat(engine, messages, max_tokens=600)
            row["latency_s"] = round(time.time() - t0, 2)
            row["completion_ok"] = bool(text1)
            row["sample"] = text1[:120]
            # Second call, same prefix, different volatile tail → cache should hit.
            messages2 = [messages[0],
                         {"role": "user", "content": "In one short sentence: a storm hits the harbor. What breaks?"}]
            t1 = time.time()
            _, u2 = openrouter_chat(engine, messages2, max_tokens=600)
            row["latency2_s"] = round(time.time() - t1, 2)
            row["prompt_tokens"] = u2.get("prompt_tokens")
            row["cached_tokens"] = cached_tokens_from_usage(u2)
            row["cache_hit"] = row["cached_tokens"] > 0
            row["cost_call1"] = u1.get("cost")
            row["cost_call2"] = u2.get("cost")
            # Typical-turn cost model: 20k in / 1.5k out; cached case assumes the
            # stable prefix + prior history (~85% of input) reads from cache.
            row["turn_cost_uncached"] = round(turn_cost(engine, 20000, 1500), 4)
            row["turn_cost_cached"] = round(turn_cost(engine, 20000, 1500, cached_tokens=17000), 4)
        except Exception as e:
            row["ok"] = False
            row["error"] = str(e)[:300]
        results.append(row)
    return results
