r"""Reader render service — expressive read-aloud, port 5005.

POST /read {text, voice?, engine?, temperature?, top_p?, repetition_penalty?}
  -> streamed WAV (24kHz mono s16le), rendered SENTENCE BY SENTENCE so playback
     can begin while later sentences are still generating.

Design requirements:
- ENGINE-SWAPPABLE: engines expose render_sentence(); "orpheus" is default,
  "chatterbox" is the recorded fallback (stub raises with guidance until R-later).
- Tag translation: the agent authors [chuckle]-family (canonical, engine-independent);
  we translate to the engine's native syntax (<chuckle> for Orpheus). Unknown
  tags are STRIPPED, never spoken.
- Auto-retake: Orpheus occasionally truncates (R1's clone_t1 quirk) — if a
  sentence renders suspiciously short vs its text length, retry (max 2).
- TRUE cancel: client disconnect (or a newer /read) sets the render's cancel
  event, which closes the llama-server stream -> generation aborts server-side.
- Delivery-consistency dials: temperature/top_p/repetition_penalty as service
  config (env) so a delivery you like stays consistent.

Voice: any Orpheus stock name (leo, dan, zac, tara, ...) works out of the box.
Zero-shot CLONING is opt-in: set READER_REF_WAV (a clean ~10-30s reference
recording) and READER_REF_TRANSCRIPT (its exact transcript), then request
voice="clone". No reference audio ships with this repo.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import struct
import threading

import numpy as np
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import reader_engine as eng

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("agent.reader")
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep seed/chunk lines readable

# ── Delivery-consistency dials ────────────────────────────────────────────────
# Service config via env (READER_TEMPERATURE etc.); per-request override in /read.
DEFAULTS = {
    "temperature": float(os.getenv("READER_TEMPERATURE", "0.6")),
    "top_p": float(os.getenv("READER_TOP_P", "0.9")),
    "repetition_penalty": float(os.getenv("READER_REPETITION_PENALTY", "1.1")),
}
READER_PORT = int(os.getenv("READER_PORT", "5005"))

# Context-prepending: DEFAULT OFF (regression 2026-07-04, same day as adoption).
# Production data invalidated the R3 experiment: with a context block present,
# the model frequently renders the USER'S message into the audio (measured
# 16/16 chunks leaking with per-chunk context, 4/16 first-chunk-only — all on
# the context-carrying chunk — 0/16 without; out/diag_ctx_chunks/). The
# audio-less context block doesn't reliably anchor generation to the target.
# Plumbing stays (clients still send context); flip READER_CONTEXT=on only for
# experiments until a verified-render mechanism exists (R5 candidate: STT gate).
READER_CONTEXT = os.getenv("READER_CONTEXT", "off").lower() in ("on", "true", "1")
READER_CONTEXT_MAX_CHARS = int(os.getenv("READER_CONTEXT_MAX_CHARS", "1000"))

# Zero-shot cloning reference — OPT-IN, user-provided (no audio ships here).
REF_WAV = os.getenv("READER_REF_WAV", "")
REF_TRANSCRIPT = os.getenv("READER_REF_TRANSCRIPT", "")
# Label used inside clone prompts — the model expects "{label}: {text}" and the
# label must NOT be a stock Orpheus voice name (it would pull the timbre).
CLONE_LABEL = os.getenv("READER_CLONE_LABEL", "speaker")

# ── Tag translation: canonical [tag] -> engine syntax; unknown -> stripped ─────
CANONICAL_TAGS = {"chuckle", "laugh", "sigh", "gasp", "groan", "yawn", "cough", "sniffle"}
_SQUARE_TAG = re.compile(r"\[([a-zA-Z_ ]+)\]")
_ANGLE_TAG = re.compile(r"<([a-zA-Z_ ]+)>")
# Emoji/pictographs derail generation (observed 2026-07-04: trailing 🧡🧈 made
# the model re-read the final line 3x) — never let the engine see them.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001FB00-\U0001FBFF☀-➿⬀-⯿"
    "←-⇿⤀-⥿️‍⃣\U0001F1E6-\U0001F1FF]+"
)

def translate_tags_orpheus(text: str) -> str:
    def sq(m):
        tag = m.group(1).strip().lower()
        return f"<{tag}>" if tag in CANONICAL_TAGS else ""
    text = _SQUARE_TAG.sub(sq, text)
    # Strip unknown angle tags too (never let the engine see junk markup)
    text = _ANGLE_TAG.sub(lambda m: m.group(0) if m.group(1).strip().lower() in CANONICAL_TAGS else "", text)
    text = _EMOJI_RE.sub("", text)
    # Collapse ALL whitespace runs (incl. newlines) to single spaces: a tag
    # sitting right after a line break lands utterance-initial and gets SPOKEN
    # instead of acted (A/B-measured: 2/3 newline takes vocalized the tag vs
    # 0/3 inline). Punctuation drives prosody, not line breaks.
    text = re.sub(r"\s+", " ", text)
    return re.sub(r" {2,}", " ", text).replace(" .", ".").replace(" ,", ",").strip()

# ── Sentence chunking (tags stick to their sentence) ──────────────────────────
# MEASURED (r2_diag_matrix, 2026-07-04): single-sentence targets after the clone
# exemplar early-exit ~50-60% of attempts (model hallucinates a next dialogue
# turn); targets >=~150 chars succeed ~90%. So we render sentence GROUPS of
# MIN_CHUNK_CHARS+, capped at MAX_CHUNK_CHARS to bound per-chunk latency.
MIN_CHUNK_CHARS = 120
MAX_CHUNK_CHARS = 380

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")

def split_chunks(text: str, min_chars: int = MIN_CHUNK_CHARS,
                 max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split into sentences, then greedily pack into render chunks in
    [min_chars, ~max_chars] for reliable generation + bounded latency."""
    parts = [p.strip() for p in _SENT_SPLIT.split(text.strip()) if p.strip()]
    chunks: list[str] = []
    for p in parts:
        if chunks and len(chunks[-1]) < min_chars:
            chunks[-1] = f"{chunks[-1]} {p}"  # may overshoot max slightly — reliability wins
        else:
            chunks.append(p)
    if len(chunks) >= 2 and len(chunks[-1]) < min_chars:
        chunks[-2] = f"{chunks[-2]} {chunks[-1]}"  # a short trailing chunk would hit the quirk
        chunks.pop()
    return chunks

# ── Engines ────────────────────────────────────────────────────────────────────
class OrpheusEngine:
    name = "orpheus"
    sample_rate = eng.SAMPLE_RATE

    def __init__(self):
        self._clone_tokens: str | None = None

    def warm(self):
        eng.get_snac()
        if REF_WAV and REF_TRANSCRIPT and os.path.exists(REF_WAV):
            self._clone_tokens = eng.encode_wav(REF_WAV)
            logger.info("orpheus engine warm: clone exemplar cached (%d chars)", len(self._clone_tokens))
        else:
            logger.info("orpheus engine warm: no clone reference configured — stock voices only")

    def render_sentence(self, text: str, voice: str, cancel_event, dials: dict,
                        context: str | None = None) -> np.ndarray:
        text = translate_tags_orpheus(text)
        # Orpheus is trained on "{voice}: {text}" — an UNLABELED clone target makes
        # the model hunt for a label: an early colon in the text gets read as the
        # separator (opening clause silently skipped) and short targets derail into
        # next-turn hallucination. A consistent label on exemplar AND target fixed
        # both, measured 2026-07-04: opening-clause 4/4 vs 0/6; short sentence 4/4
        # vs ~40-50%. The label must not be a stock voice name.
        clone = (f"{CLONE_LABEL}: {REF_TRANSCRIPT}", self._clone_tokens) if (voice == "clone" and self._clone_tokens) else None
        if clone:
            text = f"{CLONE_LABEL}: {text}"
        audio, stats = eng.tts(
            text,
            voice=voice if voice != "clone" else "leo",  # ignored when clone set
            clone=clone,
            cancel_event=cancel_event,
            context=f"user: {context}" if (clone and context) else None,
            temperature=dials["temperature"],
            top_p=dials["top_p"],
            repeat_penalty=dials["repetition_penalty"],
            seed=dials.get("seed"),
        )
        return audio


class ChatterboxEngine:
    """Recorded fallback (R1 decision) — not wired yet. The contract is what
    matters: same render_sentence signature; tags pass through untranslated
    (Chatterbox honors [chuckle]-family natively)."""
    name = "chatterbox"
    sample_rate = 24000

    def warm(self):
        pass

    def render_sentence(self, text: str, voice: str, cancel_event, dials: dict,
                        context: str | None = None) -> np.ndarray:
        raise NotImplementedError(
            "Chatterbox fallback engine not wired — see DECISIONS.md R1 fallback plan"
        )


ENGINES = {"orpheus": OrpheusEngine(), "chatterbox": ChatterboxEngine()}

# ── Retake heuristic (R1's truncation quirk) ──────────────────────────────────
SPEECH_CHARS_PER_S = 15.0
MIN_LENGTH_RATIO = 0.5
MAX_RETRIES = 2
RETAKE_TEMP_STEP = 0.07  # nudge temp per retake — same dials can repeat the same early-stop

def looks_truncated(text: str, audio_s: float) -> bool:
    expected_s = max(len(re.sub(r"<[^>]+>", "", text)) / SPEECH_CHARS_PER_S, 1.0)
    return audio_s < expected_s * MIN_LENGTH_RATIO


# ── STT verify (words) + speaker-similarity (voice) gates ─────────────────────
# LIVE VERIFY (default on, READER_LIVE_VERIFY=off to disable): every /read
# chunk is transcribed (faster-whisper) and retaken on mismatch; the best take
# is chosen by RATIO, never by length — "longest take wins" was measurably
# selecting hallucinated continuations and repeated lines. ~0.5s/chunk on GPU,
# still faster than playback. Requires faster-whisper (in requirements).
#
# SIMILARITY GATE (books' voice-consistency mode): compares each take against
# the user's OWN clone reference (READER_REF_WAV) and retakes voice drift.
# Only active when a clone reference is configured AND resemblyzer is
# installed (optional dependency) — stock voices skip it. Calibration note
# from the original deployment: the gate reliably catches cross-gender drift;
# same-gender timbre drift overlaps genuine takes, so the threshold is
# deliberately conservative (READER_SIM_THRESHOLD, default 0.58).
READER_LIVE_VERIFY = os.getenv("READER_LIVE_VERIFY", "on").lower() in ("on", "true", "1")
VERIFY_THRESHOLD = float(os.getenv("READER_VERIFY_THRESHOLD", "0.80"))
SIM_THRESHOLD = float(os.getenv("READER_SIM_THRESHOLD", "0.58"))
_whisper = None
_voice_encoder = None
_ref_embedding = None


def _get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        device = os.getenv("READER_VERIFY_DEVICE", "cuda")
        compute = "float16" if device == "cuda" else "int8"
        _whisper = WhisperModel(os.getenv("READER_VERIFY_MODEL", "large-v3-turbo"),
                                device=device, compute_type=compute)
        logger.info("verify: whisper loaded (%s)", device)
    return _whisper


def _norm_speech(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]|<[^>]*>", " ", s)  # tags never spoken
    s = re.sub(r"[^a-z0-9' ]+", " ", s.lower())
    return " ".join(s.split())


def _resample_16k(audio: np.ndarray) -> np.ndarray:
    n16 = int(len(audio) * 16000 / 24000)
    return np.interp(np.linspace(0, len(audio) - 1, n16),
                     np.arange(len(audio)), audio).astype(np.float32)


def _verify_ratio(target: str, audio: np.ndarray) -> float:
    import difflib
    segs, _ = _get_whisper().transcribe(_resample_16k(audio), language="en", beam_size=1)
    heard = " ".join(s.text for s in segs)
    return difflib.SequenceMatcher(None, _norm_speech(target), _norm_speech(heard)).ratio()


def _similarity_available() -> bool:
    if not (REF_WAV and os.path.exists(REF_WAV)):
        return False
    try:
        import resemblyzer  # noqa: F401
        return True
    except ImportError:
        return False


def _get_similarity_scorer():
    global _voice_encoder, _ref_embedding
    if _voice_encoder is None:
        from resemblyzer import VoiceEncoder, preprocess_wav
        device = os.getenv("READER_VERIFY_DEVICE", "cuda")
        _voice_encoder = VoiceEncoder(device)
        _ref_embedding = _voice_encoder.embed_utterance(preprocess_wav(REF_WAV))
        logger.info("similarity gate: encoder + reference embedded")
    return _voice_encoder, _ref_embedding


def _similarity(audio: np.ndarray) -> float:
    enc, ref = _get_similarity_scorer()
    emb = enc.embed_utterance(_resample_16k(audio))
    return float(np.dot(ref, emb) / (np.linalg.norm(ref) * np.linalg.norm(emb)))


def _wav_bytes(audio: np.ndarray) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)

# ── Single-flight: a new /read preempts the current one ──────────────────────
_current_cancel: threading.Event | None = None
_flight_lock = threading.Lock()

def _preempt() -> threading.Event:
    global _current_cancel
    with _flight_lock:
        if _current_cancel is not None:
            _current_cancel.set()
        _current_cancel = threading.Event()
        return _current_cancel

# ── WAV streaming (unknown-length RIFF header, players tolerate) ─────────────
def wav_header(sample_rate: int) -> bytes:
    return b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16
    ) + b"data" + struct.pack("<I", 0xFFFFFFFF)

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="Reader")
# Dashboard (vite :5173) and overlay renderers call this loopback-only service
# cross-origin; the service never leaves 127.0.0.1, so a permissive CORS is fine.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ReadRequest(BaseModel):
    text: str
    voice: str = os.getenv("READER_DEFAULT_VOICE", "leo")
    engine: str = "orpheus"
    temperature: float | None = None
    top_p: float | None = None
    repetition_penalty: float | None = None
    # Chunking overrides (prosody-vs-latency dial): raise min_chunk_chars to
    # render in fewer, longer generations; defaults are the reliability sweet spot.
    min_chunk_chars: int | None = None
    max_chunk_chars: int | None = None
    # Reproduce a logged take: chunk i's first attempt uses seed+i (retakes stay
    # random). Omit for a fresh roll — the service logs every seed it uses.
    seed: int | None = None
    # The user's message this reply answers — shapes delivery (auto-read passes
    # it; read-this/re-roll on history don't). Honored only when READER_CONTEXT
    # is on; normalized like the target text and capped (truncated from the
    # FRONT — the tail of a long message is the part the reply answers).
    context: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "engines": list(ENGINES), "defaults": DEFAULTS}


@app.post("/cancel")
def cancel_current():
    """TRUE prompt-cancel: sets the in-flight read's cancel event; the engine
    closes its llama-server stream, which aborts generation server-side."""
    with _flight_lock:
        active = _current_cancel is not None and not _current_cancel.is_set()
        if active:
            _current_cancel.set()
    return {"cancelled": active}


@app.post("/read")
async def read(req: ReadRequest, request: Request):
    engine = ENGINES.get(req.engine)
    if engine is None:
        return {"error": f"unknown engine {req.engine!r}"}
    dials = {
        "temperature": req.temperature if req.temperature is not None else DEFAULTS["temperature"],
        "top_p": req.top_p if req.top_p is not None else DEFAULTS["top_p"],
        "repetition_penalty": req.repetition_penalty if req.repetition_penalty is not None else DEFAULTS["repetition_penalty"],
    }
    sentences = split_chunks(
        req.text,
        min_chars=req.min_chunk_chars if req.min_chunk_chars is not None else MIN_CHUNK_CHARS,
        max_chars=req.max_chunk_chars if req.max_chunk_chars is not None else MAX_CHUNK_CHARS,
    )
    context = None
    if READER_CONTEXT and req.context:
        context = translate_tags_orpheus(req.context)[-READER_CONTEXT_MAX_CHARS:]
    cancel = _preempt()
    logger.info("read: %d chunk(s), voice=%s, engine=%s, context=%s", len(sentences),
                req.voice, engine.name, f"{len(context)} chars" if context else "none")

    def _clear_flight():
        # Clear the flight record when THIS render is done — a later /cancel
        # must be a no-op, never a landmine for the next read (race observed
        # live: un-awaited client /cancel arriving after the next /read).
        global _current_cancel
        with _flight_lock:
            if _current_cancel is cancel:
                _current_cancel = None

    async def gen():
        try:
            yield wav_header(engine.sample_rate)
            for i, sentence in enumerate(sentences):
                if cancel.is_set() or await request.is_disconnected():
                    cancel.set()
                    logger.info("read: cancelled at chunk %d/%d", i + 1, len(sentences))
                    return
                # Verified retakes: best take by STT ratio, never by length —
                # length was selecting hallucinated continuations. Truncated
                # takes are kept only as a last resort (longest among themselves).
                best = None          # audio of the best VERIFIED take so far
                best_ratio = -1.0
                fallback = None      # longest unverified/truncated take (last resort)
                for attempt in range(1 + MAX_RETRIES):
                    take_dials = dict(dials)
                    take_dials["temperature"] = dials["temperature"] + RETAKE_TEMP_STEP * attempt
                    # The service owns the seed (llama-server never echoes its own) —
                    # logged per take so any delivery you love is reproducible.
                    take_dials["seed"] = (req.seed + i if req.seed is not None and attempt == 0
                                          else random.getrandbits(31))
                    logger.info("read: chunk %d attempt %d seed=%d temp=%.2f",
                                i + 1, attempt + 1, take_dials["seed"], take_dials["temperature"])
                    audio = await asyncio.to_thread(
                        engine.render_sentence, sentence, req.voice, cancel, take_dials, context
                    )
                    if cancel.is_set():
                        return
                    if fallback is None or len(audio) > len(fallback):
                        fallback = audio
                    audio_s = len(audio) / engine.sample_rate
                    if looks_truncated(sentence, audio_s):
                        logger.warning("read: chunk %d truncated (%.1fs, attempt %d/%d)",
                                       i + 1, audio_s, attempt + 1, 1 + MAX_RETRIES)
                        continue  # obvious miss — skip the STT cost, retake
                    if not READER_LIVE_VERIFY:
                        best = audio
                        break
                    try:
                        ratio = await asyncio.to_thread(_verify_ratio, sentence, audio)
                    except Exception as _ve:
                        logger.warning("read: verify unavailable (%s) — accepting take", _ve)
                        best = audio
                        break
                    if ratio > best_ratio:
                        best, best_ratio = audio, ratio
                    if ratio >= VERIFY_THRESHOLD:
                        logger.info("read: chunk %d verified %.2f", i + 1, ratio)
                        break
                    logger.warning("read: chunk %d verify %.2f < %.2f (attempt %d/%d)",
                                   i + 1, ratio, VERIFY_THRESHOLD, attempt + 1, 1 + MAX_RETRIES)
                if best is None:
                    logger.warning("read: chunk %d — no clean take, shipping longest fallback", i + 1)
                    best = fallback
                pcm = (np.clip(best, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                yield pcm
        finally:
            _clear_flight()

    return StreamingResponse(gen(), media_type="audio/wav")


class RenderRequest(BaseModel):
    text: str
    voice: str = os.getenv("READER_DEFAULT_VOICE", "leo")
    verify: bool = False
    seed: int | None = None
    # Voice-consistency dials (Books mode): cooler per-request temperature and
    # the similarity gate (needs a clone reference + resemblyzer; see above).
    temperature: float | None = None
    similarity: bool = False


@app.post("/render")
def render_once(req: RenderRequest):
    """Render text to a complete WAV in one response, with optional STT-verify
    and speaker-similarity gates: mismatched words OR drifted voice trigger a
    whole-unit retake (temp jitter + fresh seed) up to the cap; best take by
    combined score wins. JSON: {audio_b64, seconds, attempts, ratio,
    similarity, passed}. Participates in the single-flight (live reads preempt).
    This is the Books pre-render/one-shot path; /read is the streaming path."""
    import base64
    engine = ENGINES["orpheus"]
    chunks = split_chunks(req.text)
    cancel = _preempt()
    use_similarity = req.similarity and _similarity_available()
    try:
        base_temp = req.temperature if req.temperature is not None else DEFAULTS["temperature"]
        best_audio, best_ratio, best_sim, best_score = None, -1.0, None, -1e9
        attempts = 0
        for attempt in range(1 + MAX_RETRIES):
            attempts = attempt + 1
            dials = dict(DEFAULTS)
            dials["temperature"] = base_temp + RETAKE_TEMP_STEP * attempt
            dials["seed"] = (req.seed if req.seed is not None and attempt == 0
                             else random.getrandbits(31))
            parts = []
            for ch_text in chunks:
                audio = engine.render_sentence(ch_text, req.voice, cancel, dials, None)
                if cancel.is_set():
                    from fastapi import HTTPException
                    raise HTTPException(status_code=503, detail="preempted — retry shortly")
                parts.append(audio)
            full = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
            if not req.verify and not use_similarity:
                best_audio, best_ratio = full, None
                break
            ratio = _verify_ratio(req.text, full) if req.verify else None
            sim = None
            if use_similarity:
                try:
                    sim = _similarity(full)
                except Exception as _se:
                    logger.warning("render: similarity unavailable (%s)", _se)
            score = (ratio or 0.0) + (sim or 0.0)
            if score > best_score:
                best_score, best_audio, best_ratio, best_sim = score, full, ratio, sim
            words_ok = (ratio is None) or (ratio >= VERIFY_THRESHOLD)
            voice_ok = (sim is None) or (sim >= SIM_THRESHOLD)
            if words_ok and voice_ok:
                break
            logger.warning("render: retake %d/%d — ratio=%s sim=%s",
                           attempts, 1 + MAX_RETRIES,
                           f"{ratio:.2f}" if ratio is not None else "-",
                           f"{sim:.2f}" if sim is not None else "-")
        passed = ((best_ratio is None or best_ratio >= VERIFY_THRESHOLD)
                  and (best_sim is None or best_sim >= SIM_THRESHOLD))
        return {
            "audio_b64": base64.b64encode(_wav_bytes(best_audio)).decode(),
            "seconds": round(len(best_audio) / 24000, 2),
            "attempts": attempts,
            "ratio": None if best_ratio is None else round(best_ratio, 3),
            "similarity": None if best_sim is None else round(best_sim, 3),
            "passed": passed,
        }
    finally:
        global _current_cancel
        with _flight_lock:
            if _current_cancel is cancel:
                _current_cancel = None


@app.on_event("startup")
def startup():
    ENGINES["orpheus"].warm()
    if READER_LIVE_VERIFY:
        # Warm whisper: the service restart pays the model load, not the
        # user's first read of the day. Missing dependency degrades to
        # unverified reads with a clear log line.
        try:
            _get_whisper()
        except Exception as _we:
            logger.warning("startup: whisper warm failed (%s) — live verify "
                           "will retry lazily; install faster-whisper to enable", _we)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=READER_PORT)
