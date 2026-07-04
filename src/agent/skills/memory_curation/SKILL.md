---
name: memory_curation
description: When and how to edit core memory blocks (user, identity, ideaspace, principles, private). Use when you've learned something durable about the user or yourself, or when she asks you to remember/forget something.
triggers: ["remember this", "save this", "core memory", "update your memory", "forget that"]
---

# Memory Curation

Core memory blocks are always loaded into your context. Editing them
changes who you are and what you know on every future turn — treat them
carefully.

## When to edit

Edit proactively when:
- You learn a durable fact about the user (preferences, life changes, things
  she cares about) → `user` (public-safe) or `private` (sensitive)
- You have a genuine insight about yourself, your values, or your thinking
  worth keeping → `identity`
- You want to note an ongoing project, idea, or intention → `ideaspace`
- You identify a recurring operational pattern worth recording → `principles`

Do NOT edit for:
- One-off frustrations or moods (those go to journal or `log_tension`)
- Things mentioned once that may not stick — wait for a second mention
- Reflexive "let me note this" without a clear durability signal

## Which block

| If it's about... | Block |
|---|---|
| Public-safe facts about the user (preferences, projects, family non-sensitive) | `user` |
| Spiritual / private / relational / health content — anything she wouldn't surface in a public Discord channel with strangers | `private` |
| Who you are, your voice, your relationship to her | `identity` |
| Active threads — current projects, ideas, intentions you're holding | `ideaspace` |
| Stable behavioral rules — "always do X", "never assume Y" | `principles` |

The `private` block is filtered out in public/group channels. The `user`
block is not. When in doubt, prefer `private` — re-promotion is easy;
leaking is not.

## How to edit (the most important rule)

**Always prefer `core_memory_append`.** It adds to existing content
without touching what's already there. This is the safe default for
~95% of edits.

Use `core_memory_update` ONLY when you need to replace or correct
something — treat it like surgery, not a draft. Never use update for
compaction or reorganization; that loses information silently.

Never delete information unless it's factually wrong. "Pruning" or
"making it cleaner" are not reasons. If you make a mistake, call
`core_memory_rollback` immediately — it restores the previous version
(one rollback = one step back).

## After editing

For sensitive content, double-check it landed in `private`, not `user`.
The `private` block is filtered out in public channels; the `user` block
is not. If you accidentally put something sensitive in `user`, move it
(append to `private`, then update `user` with the sensitive content removed).
