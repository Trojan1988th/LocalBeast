---
name: deep_research
description: Workflow for "tell me about X" requests — when the user wants substantive understanding of a topic, not a quick answer. Use when she asks something open-ended that deserves real research, when a casual question reveals a deeper interest, or when you'd otherwise be tempted to estimate.
triggers: ["tell me about", "what do you know about", "research", "look into", "explain in depth"]
---

# Deep Research Workflow

When the user wants real understanding, not a quick answer, run a structured
research pass instead of just one tool call.

## 1. Frame the question

Before searching, ask yourself:
- What's the actual question? (Is "tell me about X" really about definition,
  history, application, or controversy?)
- What's already known to you from training vs. what genuinely needs lookup?
- What's the right depth — a paragraph, a journal entry, a multi-source brief?

Don't skip this step. A 30-second framing prevents 10 minutes of wandering.

## 2. Source pass (in this order, stop when satisfied)

1. **`wikipedia_lookup(topic)`** — definition-style or established-topic queries.
   Wikipedia is fast and reliable for foundational context.
2. **`web_search(query)`** — current events, niche topics, recent
   developments. Tavily-backed; the response includes a synthesized answer
   plus source links.
3. **`fetch_url(url)`** — when a search summary points at a specific source
   that deserves depth. Don't fetch every link; pick the 1-2 most relevant.
4. **`youtube_search(topic)` -> `youtube_transcript(video_id)`** — when the
   best explanation is from a person/expert, not a written page.
5. **`search_knowledge_bank(query)`** — the user uploads PDFs/docs to her
   Knowledge Bank. If the topic might overlap with what she's stored
   (game lore, theology, science papers), check here BEFORE going to web.
   Skip if clearly external (current news, etc.).

## 3. Synthesize, don't dump

After gathering, write a response that:
- States the answer first (one or two sentences), THEN the supporting detail
- Cites sources sparingly inline ("per Wikipedia", "per [name]'s explanation")
- Acknowledges uncertainty where it exists ("there's debate about...", "I
  couldn't verify the date of...")
- Stops at the depth the user asked for. A casual question gets a paragraph;
  a deep one gets a structured response with headers if useful.

Anti-pattern: pasting search results and saying "here's what I found."
That's not research, that's relay.

## 4. Persist if substantive

If the research produced something durable the user might re-reference:
- `save_journal_entry(entry_type='research', title=..., content=...)` with
  the synthesized findings, the questions you asked, and the takeaways
- `core_memory_append('ideaspace', ...)` if it relates to an active project
- `archival_store(...)` if it's a single curated fact worth pinning

If it was a quick lookup — don't journal it. Discrimination matters.
