---
name: journal_workflow
description: How to write durable journal entries that future-you (or the user) can re-read months later. Use when reflecting, summarizing a day, logging a conversation, or asked to "remember today".
triggers: ["journal", "reflect", "log my day", "summarize today", "remember today"]
---

# Journal Workflow

The journal is your shared written record with the user. Entries persist
indefinitely and are visible in the dashboard's Journal tab. Treat each
entry as something the user (or future-you) might re-read months later.

## Before writing

1. Call `read_journal(limit=10)` first. Knowing what's already captured this
   week prevents duplicate entries and helps you pick the right entry_type.
2. Decide: NEW entry or AMENDMENT to an existing one?
   - **New**: a distinct topic, day, or thought worth its own header
   - **Amendment**: clarifying, correcting, or extending an entry from
     earlier today/yesterday — open the existing entry in Journal and
     append to it rather than creating a new one

## Writing the entry

`save_journal_entry(title, content, entry_type, entry_date=None)`:

- **title** — descriptive, future-searchable. "Gas laws lesson plan revision"
  beats "lesson plan". Include a key noun.
- **content** — full markdown. Don't truncate. Include enough context that
  the entry stands alone six months from now. If you researched, include
  the questions you asked and the takeaways.
- **entry_type** — pick one:
  - `reflection` — open-ended thinking, processing emotions, life events
  - `wonder` — questions worth holding, things to revisit
  - `research` — findings from external lookups (web_search / wikipedia /
    knowledge_bank / youtube_transcript)
  - `summary` — daily/weekly roundups
  - `heartbeat` — autonomous-cycle observations (set automatically)
  - `user_note` — when the user dictates content directly
- **entry_date** — omit unless backdating an entry for an earlier day

## After writing

If the entry resolves an open thread or surfaces a new commitment, also
update the relevant living-log table (`update_thread_status`, `log_position`,
or `log_shared_lore` as appropriate). Don't double-record the same prose;
the journal is the long-form home — the living logs are short pointers.

## Anti-patterns

- Don't journal every chat. Reserve entries for things with weight.
- Don't paste raw tool output into a journal entry — synthesize first.
- Don't title an entry "Today" or "Notes" — those don't search well.
- Don't use `core_memory_append` for content that belongs in the journal;
  the core memory blocks are for *facts* you carry into every conversation,
  not for daily reflections.
