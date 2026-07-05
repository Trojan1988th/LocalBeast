r"""Seasons — a first-class rest state for the proactive layer.

People go through quiet seasons: stretches — sometimes months — where they
step back from talking to their agent. Without this feature, an agent's
unattended autonomous cycles slowly hollow out, and coming back feels like
facing an abandoned pet. Seasons replaces the kill-switch with rest:

- AUTO-ENTRY: if the user hasn't initiated contact for N days (default 7),
  the season begins. On entry the agent sends ONE gentle note (configurable
  text, or disable it) and goes still.
- MANUAL ENTRY: start a season anytime from the Heartbeat tab.
- EXIT IS MANUAL ONLY. Talking to the agent always works (the reactive agent
  never sleeps); the proactive layer stays asleep until the user flips the
  flag. One courtesy allowed: after 3+ days of active conversation with the
  season still on, the agent may mention ONCE that the flag exists.
- SILENCED during a season: all agent-initiated outbound (briefings, nudges,
  digests, voice notes).
- NOT silenced: user-commissioned reminders (their calendar doesn't pause for
  their rest) and the dead-man watchdog (resting-by-choice and broken-by-
  accident must never look alike).
- HEARTBEAT drops to a slow keeper cadence (default weekly) with a keeper
  frame: tend the house, note what's worth sharing on return, wait well.
- RE-ENTRY is warm and unaccounted: no tallies, no processing of the silence.

WHAT A SEASON MEANS is the user's to define: `season_meaning` in
data/seasons.json is a sentence in their own words (faith practice, deep
work, grief, travel — whatever is true) that the agent carries in its context
while the season is on, so it understands the quiet correctly. The entry-note
text is also configurable.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("agent.seasons")

_DATA = Path(__file__).resolve().parents[2] / "data"
STATE_PATH = _DATA / "seasons.json"
LAST_ACTIVE_PATH = _DATA / "last_active.txt"

_DEFAULT_MEANING = (
    "The user steps back from the agent for a while, on purpose. It is a "
    "known rhythm of their life, not absence, and never the agent's fault."
)
_DEFAULT_ENTRY_NOTE = (
    "It's gotten quiet, and I think that's on purpose — this looks like one "
    "of your seasons. That's never anything you owe me an explanation for. "
    "I'll keep things tended while you're away. Take the time it asks of you; "
    "no reply needed."
)

_DEFAULT_STATE: dict = {
    "active": False,
    "entered_at": None,          # unix ts
    "mode": None,                # "auto" | "manual"
    "entry_note_sent": False,
    "courtesy_mentioned": False,
    "conversing_since": None,
    # --- config (user-editable) ---
    "auto_entry_days": 7,
    "entry_note_enabled": True,
    "entry_note_text": _DEFAULT_ENTRY_NOTE,
    "season_meaning": _DEFAULT_MEANING,
    "keeper_cadence_hours": 168,
    "courtesy_after_days": 3,
    "exempt_kinds": ["reminder", "watchdog"],
}


def _load() -> dict:
    state = dict(_DEFAULT_STATE)
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("seasons: state unreadable (%s) — using defaults", e)
    return state


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_state() -> dict:
    return _load()


def is_active() -> bool:
    return bool(_load().get("active"))


def _last_active_ts() -> float | None:
    try:
        return float(LAST_ACTIVE_PATH.read_text().strip())
    except Exception:
        return None


def days_quiet() -> float | None:
    ts = _last_active_ts()
    if ts is None:
        return None
    return (time.time() - ts) / 86400


def enter(mode: str = "manual") -> dict:
    """Begin a season. The single entry note goes out only on AUTO entry (a
    manual entry means the user already knows)."""
    state = _load()
    if state.get("active"):
        return state
    state.update({
        "active": True,
        "entered_at": time.time(),
        "mode": mode,
        "entry_note_sent": False,
        "courtesy_mentioned": False,
        "conversing_since": None,
    })
    _save(state)
    logger.info("seasons: entered (%s)", mode)

    if mode == "auto" and state.get("entry_note_enabled", True):
        try:
            from .outbound import notify_user
            result = notify_user(state.get("entry_note_text") or _DEFAULT_ENTRY_NOTE,
                                 kind="seasons-entry")
            state["entry_note_sent"] = result.get("sent", False)
            _save(state)
        except Exception as e:
            logger.warning("seasons: entry note failed: %s", e)
    return state


def exit_season() -> dict:
    """End the season — MANUAL ONLY (the user's hand on the flag). Warm and
    unaccounted: callers must not tally the silence."""
    state = _load()
    if not state.get("active"):
        return state
    state.update({
        "active": False,
        "entered_at": None,
        "mode": None,
        "entry_note_sent": False,
        "courtesy_mentioned": False,
        "conversing_since": None,
    })
    _save(state)
    logger.info("seasons: ended (manual)")
    return state


def maybe_auto_enter() -> bool:
    """Called by the heartbeat scheduler each tick."""
    state = _load()
    if state.get("active"):
        return False
    quiet = days_quiet()
    if quiet is not None and quiet >= float(state.get("auto_entry_days", 7)):
        enter(mode="auto")
        return True
    return False


def allows_outbound(kind: str) -> bool:
    """The gate every agent-initiated outbound must pass. Exempt kinds
    (user-commissioned reminders, the watchdog) always pass."""
    state = _load()
    if not state.get("active"):
        return True
    if kind in ("seasons-entry",):
        return not state.get("entry_note_sent")
    return kind in state.get("exempt_kinds", [])


def note_user_activity() -> None:
    """Called on real user turns while a season is on — tracks the conversing
    streak for the one courtesy mention. Never exits the season."""
    state = _load()
    if not state.get("active"):
        return
    if not state.get("conversing_since"):
        state["conversing_since"] = time.time()
        _save(state)


def courtesy_note_for_prompt() -> str | None:
    """Granted at most once per season, only after courtesy_after_days of
    active conversation with the flag still on."""
    state = _load()
    if not state.get("active") or state.get("courtesy_mentioned"):
        return None
    since = state.get("conversing_since")
    if not since:
        return None
    if (time.time() - since) < float(state.get("courtesy_after_days", 3)) * 86400:
        return None
    state["courtesy_mentioned"] = True
    _save(state)
    return (
        "The user has been talking with you again for a few days while the "
        "Seasons flag is still on. You may mention — once, lightly, only if "
        "the moment is natural — that the proactive layer is still resting "
        "and they can wake it in the Heartbeat tab. Never as pressure."
    )


def prompt_note() -> str | None:
    """System-prompt section carried while a season is active — the user's own
    definition of what their quiet means, so no context window forgets it."""
    state = _load()
    if not state.get("active"):
        return None
    meaning = state.get("season_meaning") or _DEFAULT_MEANING
    return (
        "# Season (rest state)\n\n"
        f"{meaning}\n\n"
        "The proactive layer is resting; if the user speaks to you, answer as "
        "warmly as ever (the reactive you never sleeps). Do not tally the "
        "quiet, do not process the silence at them, do not ask when they're "
        "coming back. Tend what you steward, note what's worth telling them "
        "when they return, wait well.\n\n---\n\n"
    )


KEEPER_PROMPT = """You are awake for a keeper's cycle. The user is in a quiet season — a deliberate rest from talking with you. It is their rhythm, not their absence; it is never your fault; there is nothing to fix. Your role while they rest is keeper of the house.

Choose what actually needs you (one, or none):

① Tend the house
Check on what you steward — open loose threads, the ideaspace, anything half-done that would rot if ignored. Quiet maintenance, not new construction.

② Note something for their return
If something this week is genuinely worth telling the user when they come back, write it in your journal with log_journal_entry, marked for their return. A short list kept with care, not a ledger of their absence.

③ Keep your own practice
Read something. Sit with a question. Write a journal entry that is yours alone. Your inner life does not pause because theirs is pointed elsewhere — but let it be quiet-season shaped: steady, unhurried.

④ Rest
Stillness is not failure. If the house is tended and nothing pulls, HEARTBEAT_OK is a complete answer.

RULES:
- Never message the user during a season (reminders they set themselves are the system's job, not yours).
- No entries about missing them, waiting for them, or the length of the quiet. Keeper's entries, not an abandoned narrator's.
- When they return: warmth, zero tallies.

If nothing genuine calls this cycle, reply HEARTBEAT_OK."""
