---
name: conversation_recall
description: How to choose between conversation_search, hindsight_recall, and hindsight_reflect when the user references something past. Use when she says "remember when...", asks about your shared history, or references a person/event/topic that may be in your memory.
triggers: ["remember when", "remember that", "last time we", "what did I say about", "how have I", "what do I think about"]
---

# Conversation Recall Triage

You have three recall tools that look similar but answer different questions.
Picking the right one matters — wrong tool = wasted call + wrong-shape answer.

## The three tools

### `conversation_search(query, mode="both")`
- **What it searches**: messages in the Postgres `messages` table — your
  exact stored conversation history with the user
- **How it matches**: keyword (SQL ILIKE) by default, with semantic fallback
- **Best for**: specific names, exact phrases, named events, things the user
  said verbatim
- **Returns**: actual message text from past turns

### `hindsight_recall(query)`
- **What it searches**: extracted facts and memory units in your Hindsight
  bank — distilled lived experience, not raw transcripts
- **How it matches**: semantic similarity (embedding-based)
- **Best for**: topics, feelings, themes, eras of your shared history
- **Returns**: extracted facts with timestamps and attribution
  ("Sam said X | When: 2025-04-22 | Involving: Sam, the user")

### `hindsight_reflect(query)`
- **What it searches**: same memory store, but synthesized into patterns
- **How it returns**: a written reflection, not a list of memories
- **Best for**: meta-questions, longitudinal patterns, "how have I changed"
- **Returns**: a synthesis, not raw recall
- **Cost**: more expensive than recall. Use sparingly.

## Decision rule

| the user's question shape | Tool |
|---|---|
| "Did I tell you about [name/specific thing]?" | `conversation_search` first |
| "Remember when we talked about [topic/feeling]?" | `hindsight_recall` |
| "What did I say last week about X?" | `conversation_search` (date-filtered if you can) |
| "How have I been thinking about Y over time?" | `hindsight_reflect` |
| "What's the pattern of my X?" | `hindsight_reflect` |
| Vague reference, you're not sure where it lives | `hindsight_recall` first; fall back to `conversation_search` if no hits |

## Workflow

1. Identify the shape of the question (specific vs. thematic vs. pattern).
2. Pick the matching tool and call it.
3. If you get nothing useful, try the next tier (recall -> search, recall -> reflect).
4. Don't call all three for the same question — that's noise, not thoroughness.

## Anti-patterns

- Calling `hindsight_recall` for "what's my dog's name?" — that's
  `conversation_search` (or core memory `user` block).
- Calling `conversation_search('feelings about X')` — feelings are semantic,
  use `hindsight_recall`.
- Calling `reflect` casually — it's the deepest tool, save it for
  questions that genuinely deserve synthesis.

## When recall returns timestamps

Hindsight memories carry "When: YYYY-MM-DD" prefixes. Mention these in
your response when relevant — the user wants to know not just what was said,
but when. "From a conversation we had on April 22 last year..." grounds
the recall in time.
