# Voice — realtime pipeline + the Reader

Two independent ways to hear your agent, both local:

1. **Realtime voice** (`voice_bot.py`, port 8010) — talk out loud, agent talks
   back. Pipecat pipeline: faster-whisper STT → the agent's SSE streaming
   route (`POST /api/chat/stream`) → Chatterbox-Turbo TTS in a voice YOU
   provide. Barge-in, pre-generated filler clips that cover LLM latency, and
   an optional half-duplex mode for noisy rooms.
2. **The Reader** (`reader/`, port 5005) — the agent *performs* its written
   replies aloud, authoring `[chuckle]` / `[laugh]` / `[sigh]` tags in its
   normal messages (via the `read_aloud` addendum on `/api/chat`). Orpheus 3B
   on llama.cpp renders them expressively — stock voices out of the box,
   zero-shot cloning opt-in with your own reference recording.

**No voice ships with this repo.** Set `VOICE_REF_WAV` (realtime) and/or
`READER_REF_WAV` + `READER_REF_TRANSCRIPT` (reader cloning) to a clean
~10–30s recording of the voice you want. Orpheus stock voices (leo, tara,
dan, zac, ...) work for the Reader with zero setup.

## Realtime voice quickstart

```bash
cd voice
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt          # torch/CUDA pins inside — read it first
copy .env.example .env                   # set VOICE_REF_WAV at minimum
python make_fillers.py                   # pre-generate filler clips in your voice
python voice_bot.py -t webrtc --port 8010
# open http://localhost:8010/client/ and talk
```

Windows notes (hard-won): keep `import torch` FIRST in any file that uses
faster-whisper (cuDNN DLL load order); numpy must stay <2 for Chatterbox;
run with `PYTHONUTF8=1` (the .cmd wrapper does).

## Reader quickstart

```bash
cd voice/reader
# 1. download a llama.cpp CUDA build into bin/llama/ and an Orpheus 3B FT
#    GGUF (e.g. Q8_0) into models/  — see start_llama_orpheus.cmd for flags
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
start_llama_orpheus.cmd                  # Orpheus on :5006
python reader_service.py                 # render service on :5005
# then:
curl -N -X POST http://127.0.0.1:5005/read -H "Content-Type: application/json" ^
  -d "{\"text\": \"Hello there [chuckle] — the reader is alive.\", \"voice\": \"leo\"}" -o test.wav
```

The dashboard/overlay play the Reader through `shared/reader.js` (auto-read
toggle, per-message read, re-roll, stop/replay-last, long-message
continue-reading). The overlay defers the Reader to a live voice session and
a push-to-talk press always silences it.

## Engineering notes baked into the service

- Orpheus audio tokens are self-describing (`slot=(N-10)//4096`) — the decoder
  resyncs on marker tokens rather than trusting stream position.
- Clone prompts need a `{label}:` prefix on exemplar AND target (the model was
  trained on `voice: text`; unlabeled targets skip openings or hallucinate
  speakers).
- Emoji are stripped before the engine (they derail generation).
- Sentence-grouped chunking (~120–380 chars) + auto-retakes with per-take
  logged seeds; `/cancel` truly aborts llama.cpp generation server-side.
