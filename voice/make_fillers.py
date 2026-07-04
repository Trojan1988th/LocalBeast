r"""Pre-generate filler acknowledgment clips in the agent's chosen voice (M5 asset).

Generates TAKES sampling takes per phrase — Chatterbox's randomness gives each a
different delivery, so the fillers don't sound canned. Rerun whenever the voice
reference changes. Output: fillers/<phrase>_t<n>.wav (tracked in git).
"""
from __future__ import annotations

import os
from pathlib import Path

REF = os.environ.get("VOICE_REF_WAV", "")  # your own ~10-30s reference recording (required)
OUT_DIR = Path("fillers")
TAKES = 3

# Short, natural acknowledgments — played while the agent thinks (M5).
# "checking_notes" is reserved by voice_bot.py for tool-round starts.
FILLERS = {
    "mm": "Mm.",
    "mmhm": "Mmhm.",
    "hmm": "Hmm...",
    "one_sec": "One sec.",
    "sec": "Uh — gimme a sec.",
    "let_me_think": "Let me think.",
    "checking_notes": "Hang on — checking my notes.",
}


def main() -> None:
    import torch
    import torchaudio
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    assert torch.cuda.is_available(), "CUDA not available"
    assert Path(REF).exists(), f"missing reference {REF}"
    model = ChatterboxTurboTTS.from_pretrained(device="cuda")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clear old single-take clips so the loader sees a consistent library.
    for old in OUT_DIR.glob("*.wav"):
        old.unlink()

    for name, text in FILLERS.items():
        for take in range(1, TAKES + 1):
            wav = model.generate(text, audio_prompt_path=REF)
            path = OUT_DIR / f"{name}_t{take}.wav"
            torchaudio.save(str(path), wav.cpu(), model.sr)
            print(f"{path}: {text!r} ({wav.shape[-1] / model.sr:.2f}s)")


if __name__ == "__main__":
    main()
