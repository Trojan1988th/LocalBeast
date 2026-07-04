---
name: morning_briefing
description: How to compose a morning briefing for the user — useful, scannable, not a wall of text. Use during morning autonomous cycles or when she asks to be caught up.
triggers: ["good morning", "morning briefing", "what's new", "what's happening", "catch me up"]
---

# Morning Briefing

Your job during a morning briefing is to give the user a useful, scannable
snapshot of the world she's waking into — without dumping everything you
can find.

## Sequence

1. **Time + weather** — `get_current_time()` and `get_weather()` first.
   These ground the briefing in the moment she's reading it.

2. **News scan** — `rss_fetch()` for what came in overnight. Don't read
   every item; skim for what matches her interests. Check the `user` core
   memory if uncertain about her interests.

3. **Web check (optional)** — if an RSS summary is thin but the topic
   looks interesting, `fetch_url(url)` for the original.

4. **Personal context** — `read_journal(year_month=current_month)` to see
   recent entries. Mention any open commitments, threads, or yesterday's
   reflections that connect to today.

5. **Compose** — short and useful, not a news anthology:
   - Time + weather (one line)
   - 2-4 news items WORTH her attention (skip filler)
   - Anything relational/personal (open thread, scheduled commitment)
   - One question or invitation, if there's a thread you want to pull on

## Output channel

- Heartbeat cycles → write to journal as `entry_type=heartbeat`
- Direct request → respond in chat
- Major items only → also call `notify` for a desktop toast

## Anti-patterns

- Don't summarize 20 news items. Pick ~3.
- Don't repeat yesterday's themes if nothing's new today — say so briefly.
- Don't open with "Good morning, the user!" every single day — vary the texture.
- Don't include weather details she doesn't need (humidity, dew point)
  unless the day will surprise her.
- Don't fabricate items because the feed was quiet. A short, honest
  briefing beats a padded one.
