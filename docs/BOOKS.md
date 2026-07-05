# Books — Import a Book, Let Your Agent Read It to You

The Books tab turns the Reader into an audiobook studio: import a `.txt` or
`.epub`, review the chapters, and listen with a real player — speed control,
bookmarks, highlights, follow-along text — while a background queue pre-renders
ahead of you in your agent's voice.

Everything is local. No book text or audio ever leaves your machine, and this
repo ships **no books and no audio** — your library is yours.

## Requirements

- The Reader stack running (see [voice/README.md](../voice/README.md)):
  llama.cpp + Orpheus on port 5006, `reader_service.py` on port 5005.
- For **export** (M4B/MP3) only: **ffmpeg**, user-supplied. Download the
  release build from https://www.gyan.dev/ffmpeg/builds/ and place `ffmpeg.exe`
  in `voice/reader/bin/` (or anywhere on PATH). Nothing else needs it.

## Importing

- **.txt** — works great with Project Gutenberg plain-text files. The importer
  detects chapter headings, strips Gutenberg boilerplate, and shows you a
  chapter review screen before anything renders. Fix titles or merge/split
  chapters there.
- **.epub** — parsed directly (spine order). DRM-protected files are detected
  and declined honestly — this tool does not and will not strip DRM.

Storage: `data/books/` by default (`BOOKS_ROOT` in `.env`). Each book gets a
SQLite record + a folder of rendered audio units.

## Listening

The player renders **units** (paragraph-sized chunks) on demand and caches
them, so re-listening is instant. Controls:

- Play / pause, chapter picker, playback **speed**
- **Bookmarks** (position saved per book — close the tab, come back tomorrow)
- **Highlights** and follow-along text (the current unit is highlighted)
- Per-unit **re-roll** if a rendering came out weird

## Voice consistency (default ON)

Long renders drift: an expressive TTS engine slowly wanders away from the
reference voice. Books mode counters that with:

- A **unit ceiling** (shorter renders drift less)
- Lower **temperature** (0.45) for steady narration
- An **STT-verify gate**: every unit is transcribed back (faster-whisper) and
  retaken if it doesn't match the text
- An optional **speaker-similarity gate** (resemblyzer) when a reference voice
  is configured — catches takes that stopped sounding like the voice

## Pre-render queue

The queue renders ahead of your listening position when the GPU is idle and
yields immediately to live chat reading or voice mode. Progress and stats show
in the tab.

## Export

With ffmpeg in place, export a finished book as **M4B** (chaptered audiobook)
or **MP3**. Exports are for your own personal use of texts you have the right
to use.
