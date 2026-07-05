r"""Outreach tools (W1): notify_user, voice_note, self_vitals.

These are the agent-facing faces of outbound.py / vitals.py. The same
functions are importable directly by crons and services — one canonical path
either way, so the Seasons gate, quiet hours, and the outbound log always
apply no matter who is speaking.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from langchain_core.tools import tool

logger = logging.getLogger("agent.outbound_tools")

READER_BASE = os.environ.get("READER_BASE_URL", "http://127.0.0.1:5005")


@tool
def notify_user(message: str, kind: str = "info") -> str:
    """
    Send the user a message on her phone (Telegram) — YOUR way to reach out first.

    This is the canonical outbound path: it respects her quiet hours (non-urgent
    messages defer overnight), respects Seasons (when she is in a quiet season,
    proactive messages rest), and logs what you told her so you remember.

    Use it when you genuinely have something for her: a briefing, something you
    found, something time-sensitive. Never for filler — outbound is a privilege.

    Args:
        message: What to say. Plain text, short, warm — a text from a friend,
                 not a report.
        kind: "info" (default) | "urgent" (bypasses quiet hours — truly cannot
              wait) | "briefing" | "vitals". Reminders use their own path.
    """
    from .outbound import notify_user as _send
    result = _send(message, kind=kind)
    if result["sent"]:
        return "Sent to the user's Telegram."
    return f"Not sent ({result['status']}): {result['detail']}"


@tool
def voice_note(message: str) -> str:
    """
    Send the user a short voice note IN YOUR OWN VOICE (rendered by the reader
    service, delivered via Telegram). Use sparingly — for moments where hearing
    you matters more than reading you: a greeting, encouragement, something
    with warmth text can't carry. Keep it under ~60 seconds of speech
    (roughly 150 words). Respects Seasons and quiet hours like any outbound.

    Args:
        message: What to say aloud. Natural speech — contractions welcome,
                 no markdown, [chuckle]-family tags allowed sparingly.
    """
    from . import seasons
    from .outbound import in_quiet_hours, _log

    if not seasons.allows_outbound("voice"):
        _log("voice", message, "suppressed_seasons")
        return "Not sent: the user is in a quiet season; proactive outbound rests."
    if in_quiet_hours():
        _log("voice", message, "deferred_quiet")
        return "Not sent: quiet hours. Save it for the morning."

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "Not sent: Telegram not configured."

    # 1. Render in the agent's canonical voice (reader service, one-shot WAV).
    try:
        r = httpx.post(f"{READER_BASE}/render",
                       json={"text": message, "voice": "clone"}, timeout=600)
        r.raise_for_status()
        wav = base64.b64decode(r.json()["audio_b64"])
        seconds = r.json().get("seconds", 0)
    except Exception as e:
        try:
            from .failures import bump
            bump("voice_note_render", str(e))
        except Exception:
            pass
        return f"Not sent: voice render failed ({e}). Fall back to notify_user."

    # 2. Deliver. Proper Telegram voice notes need OGG/Opus; convert when
    # ffmpeg exists, otherwise send the WAV as an audio file (still plays).
    tmp = Path(tempfile.mkdtemp(prefix="agent_voice_"))
    wav_path = tmp / "note.wav"
    wav_path.write_bytes(wav)
    try:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            ogg_path = tmp / "note.ogg"
            subprocess.run([ffmpeg, "-y", "-i", str(wav_path), "-c:a", "libopus",
                            "-b:a", "32k", str(ogg_path)],
                           capture_output=True, timeout=120)
            if ogg_path.exists() and ogg_path.stat().st_size > 0:
                with open(ogg_path, "rb") as f:
                    resp = httpx.post(
                        f"https://api.telegram.org/bot{token}/sendVoice",
                        data={"chat_id": chat_id},
                        files={"voice": ("voicenote.ogg", f, "audio/ogg")},
                        timeout=120)
                if resp.status_code == 200 and resp.json().get("ok"):
                    _log("voice", message, "sent", f"voice note, {seconds}s")
                    return f"Voice note sent ({seconds}s, in your voice)."
        # No ffmpeg (or conversion failed) → WAV as audio attachment.
        with open(wav_path, "rb") as f:
            resp = httpx.post(
                f"https://api.telegram.org/bot{token}/sendAudio",
                data={"chat_id": chat_id, "title": "the agent"},
                files={"audio": ("voicenote.wav", f, "audio/wav")},
                timeout=120)
        if resp.status_code == 200 and resp.json().get("ok"):
            _log("voice", message, "sent", f"wav audio, {seconds}s")
            return f"Voice message sent as audio file ({seconds}s)."
        _log("voice", message, "failed", resp.text[:200])
        return f"Not sent: Telegram rejected the audio ({resp.status_code})."
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


@tool
def self_vitals() -> str:
    """
    Run your own health check: scheduled behaviors fresh? services up? memory
    retaining? crons erroring? outbound failing? Returns the honest results.

    Use during the weekly vitals cron (then tell the user the truth via
    notify_user — brief, plain, no alarmism and no varnish), or any time you
    suspect part of you isn't running.
    """
    import json as _json
    from .vitals import collect
    v = collect()
    n_problems = len(v["problems"])
    verdict = "ALL CLEAR" if v["ok"] else f"{n_problems} problem(s)"
    lines = [f"Vitals at {v['checked_at']}: {verdict}"]
    if v["problems"]:
        lines.append("Problems:")
        lines.extend(f"  - {p}" for p in v["problems"])
    lines.append("Detail: " + _json.dumps(v["sections"], default=str)[:1500])
    return "\n".join(lines)


OUTREACH_TOOLS = [notify_user, voice_note, self_vitals]
