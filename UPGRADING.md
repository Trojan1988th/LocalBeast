# Upgrading

Additive upgrades only — `git pull` and follow the steps for the version span
you're crossing. No history rewrites, ever.

## 2026-07 — Books, RPG, Reflections, and the Proactive Layer

This release adds the Books tab, the RPG tab (with the secret-keeping
Director), the Reflections tab, Seasons, durable reminders, outbound
notifications, the watchdog, self-vitals, calendar visibility, and brings the
Reader's voice-quality fixes (live STT-verify gate, book voice-consistency
mode) plus dashboard reader controls (auto-read / read-this / re-roll / 💾
download).

In order:

1. **Pull + reinstall Python deps**
   ```
   git pull
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Rebuild the dashboard**
   ```
   cd dashboard
   npm install --legacy-peer-deps
   npm run build     # or keep using npm run dev
   ```

3. **New env vars** (all optional — features degrade gracefully without them;
   see `.env.example` for the annotated list):
   - `BOOKS_ROOT` (default `data/books`)
   - `RPG_ROOT` (default `data/rpg`), `RPG_BANK_ID` (default `story-dm`),
     `OPENROUTER_API_KEY` (or paste in the RPG tab), `DIRECTOR_BASE`
   - `CALENDAR_ICS_URL` (read-only ICS subscription)
   - `REFLECTIONS_BOOK_ID`

4. **Database**: no manual migrations. New tables (`reminders`,
   `reflections`, `outbound_log`) are created automatically on first use.
   New JSON stores (`data/seasons.json`, `data/outbound.json`) likewise.

5. **New services (optional)**:
   - **Director** (needed only for RPG Mystery mode):
     `director/start_director.cmd` — generates its encryption key on first
     run. Consider a Task Scheduler entry if you'll use Mystery mode often.
   - **Watchdog** (recommended): `scripts/run_watchdog.cmd` via Task
     Scheduler — an independent dead-man's switch on the heartbeat.
   - **Heartbeat scheduler**: if you run it via Task Scheduler,
     `scripts/start_heartbeat.cmd` is the entry point; it now writes
     freshness stamps the watchdog and vitals read.

6. **Reader (voice) users**: the reader service gained a live STT-verify
   gate (`READER_LIVE_VERIFY`, default on) and a `/render` endpoint used by
   Books pre-rendering. If you use the verify gate, ensure `faster-whisper`
   is installed in the reader venv; the optional speaker-similarity gate
   needs `resemblyzer` (pin `setuptools<81` — a `pkg_resources` removal
   breaks `webrtcvad` otherwise). Both degrade gracefully if absent.

7. **Books export** (optional): drop `ffmpeg.exe` into `voice/reader/bin/`
   (gyan.dev release build) to enable M4B/MP3 export. Nothing else needs it.

Existing threads, memory, cron jobs, and overlay settings are untouched.
