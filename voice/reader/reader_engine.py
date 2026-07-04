r"""Orpheus 3B engine core (Phase B R1): prompt build, token streaming, SNAC codec.

Recipe verified against Lex-au/Orpheus-FastAPI (inference.py + speechpipe.py):
  prompt  = "<|audio|>{voice}: {text}<|eot_id|>"  ->  llama-server /v1/completions
  tokens  = "<custom_token_N>" text chunks; id = N - 10 - (idx % 7) * 4096
  frames  = 7 ids -> SNAC layers: codes_0=[0], codes_1=[1,4], codes_2=[2,3,5,6]
  decode  = SNAC snac_24khz; per-frame audio window [2048:4096]; 24 kHz int16

Zero-shot cloning (audition experiment): prepend an exemplar pair —
  "<|audio|>{ref transcript}<|eot_id|>" + custom_token string of the SNAC-ENCODED
  reference audio — then the real prompt; the model continues in that voice.
Encoding inverts the id math: N = id + 10 + (idx % 7) * 4096.
"""
from __future__ import annotations

import json
import re
import time

import numpy as np
import torch
from snac import SNAC

LLAMA_URL = "http://127.0.0.1:5006/v1/completions"
SAMPLE_RATE = 24000
_TOKEN_RE = re.compile(r"<custom_token_(\d+)>")

_snac = None


def get_snac() -> SNAC:
    global _snac
    if _snac is None:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _snac = SNAC.from_pretrained("hubertsiuzdak/snac_24khz").eval().to(dev)
    return _snac


def build_prompt(text: str, voice: str) -> str:
    return f"<|audio|>{voice}: {text}<|eot_id|>"


def build_clone_prompt(ref_transcript: str, ref_token_str: str, text: str,
                       context: str | None = None) -> str:
    """Exemplar-pair prompt: reference (transcript + audio tokens) then target.

    context (adopted 2026-07-04, the user's verdict on the context experiment): an
    optional prior-turn block — pre-labeled text, e.g. "user: {their message}" —
    inserted between exemplar and target as its own <|audio|>…<|eot_id|> block
    WITHOUT audio tokens. Measured 6/6 clean (zero leakage) in the R3 experiment.
    """
    ctx_block = f"<|audio|>{context}<|eot_id|>" if context else ""
    return (
        f"<|audio|>{ref_transcript}<|eot_id|>{ref_token_str}"
        f"{ctx_block}"
        f"<|audio|>{text}<|eot_id|>"
    )


def slots_to_frames(pairs: list[tuple[int, int]]) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor] | None:
    """Assemble (slot, id) pairs into SNAC codebook tensors.

    Slots are SELF-DESCRIBING — slot = (N-10)//4096 encodes the intra-frame
    position, so we don't rely on stream position (leading marker tokens like
    <custom_token_4> would otherwise misalign a positional index; observed
    live 2026-07-04). A frame = a consecutive slot run 0..6; broken runs are
    dropped rather than glitched.
    """
    c0, c1, c2 = [], [], []
    frame: list[int] = []
    for slot, tid in pairs:
        if slot != len(frame):
            frame = []  # out-of-sequence — resync at next slot 0
            if slot != 0:
                continue
        frame.append(tid)
        if len(frame) == 7:
            c0.append(frame[0])
            c1.extend([frame[1], frame[4]])
            c2.extend([frame[2], frame[3], frame[5], frame[6]])
            frame = []
    if not c0:
        return None
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    return (
        torch.tensor(c0, device=dev).unsqueeze(0),
        torch.tensor(c1, device=dev).unsqueeze(0),
        torch.tensor(c2, device=dev).unsqueeze(0),
    )


def decode_pairs(pairs: list[tuple[int, int]]) -> np.ndarray:
    """All-at-once decode: (slot, id) pairs -> float32 mono waveform @24kHz."""
    frames = slots_to_frames(pairs)
    if frames is None:
        return np.zeros(0, dtype=np.float32)
    with torch.inference_mode():
        audio = get_snac().decode(list(frames))
    return audio.squeeze().cpu().numpy().astype(np.float32)


def encode_wav(path: str) -> str:
    """SNAC-encode a wav/mp3 into the custom_token string Orpheus expects."""
    import torchaudio

    wav, sr = torchaudio.load(path)
    wav = wav.mean(dim=0, keepdim=True)  # mono
    if sr != SAMPLE_RATE:
        wav = torchaudio.functional.resample(wav, sr, SAMPLE_RATE)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    with torch.inference_mode():
        codes = get_snac().encode(wav.unsqueeze(0).to(dev))
    c0, c1, c2 = [c.squeeze(0).squeeze(0).tolist() for c in codes]
    parts = []
    for f in range(len(c0)):
        frame = [c0[f], c1[2 * f], c2[4 * f], c2[4 * f + 1], c1[2 * f + 1], c2[4 * f + 2], c2[4 * f + 3]]
        for idx, tid in enumerate(frame):
            parts.append(f"<custom_token_{tid + 10 + idx * 4096}>")
    return "".join(parts)


def stream_generate(prompt: str, *, max_tokens: int = 8192, temperature: float = 0.6,
                    top_p: float = 0.9, repeat_penalty: float = 1.1, cancel_event=None,
                    seed: int | None = None):
    """Yield ((slot, id), first_token_time) from llama-server as they stream.

    cancel_event (threading.Event): checked per stream chunk — setting it closes
    the HTTP stream, which makes llama-server ABORT the generation (true cancel,
    not just playback stop; verified llama.cpp behavior on client disconnect).
    """
    import httpx

    payload = {
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "repeat_penalty": repeat_penalty,
        "stream": True,
    }
    if seed is not None:
        payload["seed"] = seed  # explicit -> reproducible takes; llama never echoes its own
    t0 = time.perf_counter()
    first = None
    buf = ""  # tokens can split across stream chunks — buffer the tail
    with httpx.Client(timeout=300) as client:
        with client.stream("POST", LLAMA_URL, json=payload) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if cancel_event is not None and cancel_event.is_set():
                    return  # closes the stream -> llama-server aborts generation
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                buf += (json.loads(data).get("choices") or [{}])[0].get("text", "")
                last_end = 0
                for m in _TOKEN_RE.finditer(buf):
                    n = int(m.group(1))
                    last_end = m.end()
                    if n < 10:
                        continue  # marker tokens (start/end of audio), not audio data
                    slot, tid = divmod(n - 10, 4096)
                    if slot > 6:
                        continue  # not an audio token
                    if first is None:
                        first = time.perf_counter() - t0
                    yield (slot, tid), first
                buf = buf[last_end:]


def tts(text: str, voice: str = "leo", clone: tuple[str, str] | None = None,
        cancel_event=None, context: str | None = None, **gen_kwargs) -> tuple[np.ndarray, dict]:
    """Full pipeline: text -> waveform. clone=(ref_transcript, ref_token_str).
    context: optional pre-labeled prior-turn block (clone mode only).
    Returns (audio float32 @24k, stats dict)."""
    prompt = (
        build_clone_prompt(clone[0], clone[1], text, context=context) if clone
        else build_prompt(text, voice)
    )
    t0 = time.perf_counter()
    pairs: list[tuple[int, int]] = []
    first_token = None
    for pair, first in stream_generate(prompt, cancel_event=cancel_event, **gen_kwargs):
        first_token = first
        pairs.append(pair)
    gen_s = time.perf_counter() - t0
    audio = decode_pairs(pairs)
    total_s = time.perf_counter() - t0
    stats = {
        "tokens": len(pairs),
        "first_token_s": round(first_token or 0.0, 2),
        "gen_s": round(gen_s, 2),
        "decode_s": round(total_s - gen_s, 2),
        "audio_s": round(len(audio) / SAMPLE_RATE, 2),
        "rtf": round(total_s / max(len(audio) / SAMPLE_RATE, 0.01), 2),
    }
    return audio, stats
