# RPG — a DM That Can Actually Keep a Secret

The RPG tab is a tabletop story engine with a genuinely novel property: in
**Mystery mode**, the model narrating your story *does not know the answer to
the mystery*. A second, quarantined process — the **Director** — holds the
hidden truth and checks every draft before you see it.

## The six design laws

These are the feature. They are enforced by architecture, not by prompting:

1. **The Writer never sees the secret.** The narrating model receives scene
   direction ("steer toward the greenhouse; the gardener is evasive"), never
   the underlying truth.
2. **The Director never writes prose.** It reads the Writer's draft and the
   hidden truth, and returns either "compliant" or a correction directive.
   Its output never reaches the player directly.
3. **Secrets live out of process.** The Director runs as a separate service
   (`director/director_service.py`, port 5008) with its own encrypted store.
   The main agent process cannot read them even in principle.
4. **The key is generated per install.** On first run the Director generates a
   Fernet key (`director/.director_key`, gitignored). No key ships with this
   repo; no two installs share one.
5. **Corrections are ephemeral.** A Director correction triggers one revision
   round; the directive itself never enters story history or memory.
6. **The seal is verifiable.** When a mystery is created, its truth is sealed
   with a SHA-256 hash shown on the story card. At the reveal, you can verify
   the answer was fixed from the start — the DM didn't make it up as it went.

## Story manager

- **Known mode**: collaborative storytelling, no secrets — the agent DMs from
  your instructions and lore.
- **Mystery mode**: the full Writer/Director split above.
- **Lore upload**: drop PDFs/text into a story; they're chunked + embedded
  into the knowledge DB (project-scoped) and auto-retrieved during play.
  Common lore can be shared across stories.
- **Per-story instructions** with immutable versioning, an option shelf, and
  export/view of the full campaign.

## Memory hygiene

Story turns retain to a **separate Hindsight bank** (`RPG_BANK_ID`, default
`story-dm`) tagged per story and act — never to your agent's main memory.
From normal chat, the `story_recall` tool is the one-way bridge back ("how's
our campaign going?"); inside a story, recall is automatic and story-scoped.

## Engine selector (S7)

Each story can pick its Writer/Director engine:

- **Default**: your configured local/primary model (thinking disabled for
  pace).
- **OpenRouter roster**: paste an OpenRouter key in the RPG tab (or set
  `OPENROUTER_API_KEY` in `.env`) and pick per-story engines. Per-engine
  addendum overlays adapt the DM prompt to each model's quirks, and prompts
  are **cache-shaped** (stable spine first, volatile scene direction last) so
  provider prompt caches hit turn after turn. Context-window and price
  warnings surface in the UI.

Only story-scoped content routes to an external engine — the agent's core
memory, tools, and your other conversations never leave the local path.

## The DM addendum is a template

`RPG_DM_ADDENDUM` (env) or the built-in default defines the DM's craft rules.
The shipped default is a **template with slots** — response format, pacing,
additional principles — with guidance in place. Fill it with your own table's
style; the laws above stay fixed regardless.

## Setup

1. Start the Director once: `director/start_director.cmd` (generates the key).
2. Create a story in the RPG tab; toggle Mystery if you want a secret.
3. Optional: `OPENROUTER_API_KEY` for the engine roster.
4. Optional: `RPG_ROOT` (default `data/rpg`) for story storage.
