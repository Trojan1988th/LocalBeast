r"""R1 smoke test: text -> out.wav via Orpheus 3B (llama-server) + SNAC.

Prints time-to-first-audio-token, generation time, decode time, RTF.
Requires llama-server running (see start_llama_orpheus.cmd).

Usage:
  .venv\Scripts\python reader_smoke.py [--voice leo] [--text "..."] [--out out\smoke.wav]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import soundfile as sf

import reader_engine as eng


def main() -> None:
    ap = argparse.ArgumentParser(description="Orpheus reader smoke test")
    ap.add_argument("--text", default=(
        "Hey the user, this is the reader smoke test. <chuckle> "
        "If you can hear this, Orpheus is alive on the dream machine."
    ))
    ap.add_argument("--voice", default="leo")
    ap.add_argument("--out", default="out/smoke.wav")
    args = ap.parse_args()

    audio, stats = eng.tts(args.text, voice=args.voice)
    out = Path(__file__).parent / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), audio, eng.SAMPLE_RATE)
    print(f"stats: {stats}")
    print(f"wrote {out} ({stats['audio_s']}s of audio)")


if __name__ == "__main__":
    main()
