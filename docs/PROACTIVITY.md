# The Proactive Layer — Seasons, Outreach, Reminders, and the Watchdog

A set of features that let the agent act *toward* you — and, just as
important, let it rest without rotting.

## Seasons — a first-class rest state

People go through quiet stretches — sometimes months — where they step back
from talking to their agent. Without this, unattended autonomous cycles slowly
hollow out, and coming back feels like facing an abandoned pet. Seasons
replaces the kill-switch with rest:

- **Auto-entry** after N quiet days (default 7, configurable). On auto-entry
  the agent sends ONE gentle note (text configurable, or disable it) and goes
  still.
- **Manual entry/exit** from the Heartbeat tab. **Exit is manual only** —
  talking to the agent always works (the reactive agent never sleeps), but
  the proactive layer stays asleep until you flip the flag.
- **Silenced during a season**: all agent-initiated outbound (briefings,
  nudges, digests, voice notes).
- **Not silenced**: reminders you set yourself, and the watchdog (resting-by-
  choice and broken-by-accident must never look alike).
- The heartbeat drops to a slow **keeper cadence** (default weekly) with a
  keeper frame: tend the house, note what's worth sharing on return, wait
  well. Re-entry is warm and unaccounted — no tallies of the silence.

**What a season means is yours to define.** Edit `season_meaning` and
`entry_note_text` in `data/seasons.json` — a sentence in your own words (deep
work, travel, a practice, grief — whatever is true) that the agent carries in
context while the season is on, so it understands the quiet correctly.

## Outreach — notify_user, voice notes, morning briefing

- `notify_user` is the **one canonical outbound path** (tool + internal API).
  Every send passes the Seasons gate and quiet hours, and is logged to the
  `outbound_log` table with its outcome (`sent` / `suppressed_seasons` /
  `deferred_quiet` / `failed`). Delivery is Telegram
  (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`), with desktop toast fallback.
- `voice_note` renders a short message in the agent's Reader voice and sends
  it as a Telegram voice message.
- **Morning briefing**: create a cron job (Cron tab) that prompts the agent to
  assemble weather, calendar, reminders, and anything from overnight cycles,
  and send it via `notify_user`. See `examples/CRON_JOBS_EXAMPLE.md`.
- Quiet hours and per-channel toggles live in `data/outbound.json`.

## Ministry of Reminders — durable, natural-language

Reminders that survive restarts (PostgreSQL `reminders` table, created
automatically):

- `schedule_reminder` — natural-language capture: one-shot ("tomorrow 5pm"),
  **recurring** ("every weekday at 7"), and **conditional** ("remind me when
  it's raining").
- Conditional delivery is **code-performed**: at fire time the agent evaluates
  the condition and answers with a strict verdict; the system — not the LLM —
  performs the actual send. The model can't claim "sending it now" without a
  send actually happening. Ambiguity defaults to pinging you.
- `snooze_reminder`, `cancel_reminder`, `list_reminders` round it out.
- A 30-second poller inside the API process fires due rows; user-commissioned
  reminders are **exempt from Seasons** (your calendar doesn't pause for your
  rest).

## Watchdog + self-vitals

- `scripts/watchdog.py` is a **dead-man's switch** that runs as its own
  process (Task Scheduler): if the heartbeat scheduler stops stamping
  `data/heartbeat_last.txt`, the watchdog notifies you. The agent can't be
  trusted to report its own death — something independent must.
  Start it with `scripts/run_watchdog.cmd`.
- `self_vitals` (tool + `/api/vitals`): the agent's own health check — DB
  reachability, Hindsight, heartbeat freshness, reminder queue, failure
  counters (`failures.py` tracks silent-failure bumps so problems surface
  instead of vanishing into logs).

## Calendar visibility

Set `CALENDAR_ICS_URL` (Google Calendar → Settings → your calendar → "Secret
address in iCal format" — read-only, no OAuth). The `calendar_week` tool feeds
morning briefings, weekly planning crons, and reminder scheduling around your
real commitments.

## Reflections — a quiet shared journal

A deliberately unadorned tab: you write a dated entry (optionally naming what
you read); after you save, the agent writes its own note beneath yours — a
real turn, its genuine voice, never commentary-bot. Both retain to long-term
memory tagged `reflections`.

- No streaks, no stats, no gamification. Presence, not pressure.
- **Rest days**: mark today (or a weekly pattern) and the optional evening
  nudge stays quiet, no questions asked.
- Optional evening nudge: a cron job that calls `reflections_today` and sends
  one gentle line — skips rest days, respects Seasons.
- If your practice centers on a text, you can import a public-domain edition
  via the Books tab and set `REFLECTIONS_BOOK_ID` to show your reading
  position in the tab header. (An idea, not a requirement.)

## What deliberately did NOT ship

A "watch feed" digest (scheduled read-only monitoring of an external
database/community with criteria-based notifications) exists in the private
lineage but reads infrastructure specific to its author. The generic scaffold
wasn't worth shipping half-formed; if you want one, `outbound.notify_user` +
a cron job + a read-only DB connection is the whole recipe.
