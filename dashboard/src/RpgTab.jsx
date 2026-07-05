// RPG tab: story manager (S1) + play view (S2/S3).
import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { createReader, formatTagsForDisplay } from '../../shared/reader.js'

const API_BASE = '/api'
const rpgReader = createReader() // narration playback (reuses the shared reader)

function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : 'not yet'
}

export default function RpgTab() {
  const [stories, setStories] = useState([])
  const [view, setView] = useState('list') // list | manage | play | read
  const [manageSlug, setManageSlug] = useState(null)
  const [playSlug, setPlaySlug] = useState(null)
  const [readSlug, setReadSlug] = useState(null)
  const [newTitle, setNewTitle] = useState('')
  const [newMystery, setNewMystery] = useState(false)
  const [newCommonLore, setNewCommonLore] = useState(true) // the user uses the common docs in almost every story
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/rpg/stories`)
      if (res.ok) setStories((await res.json()).stories || [])
    } catch { /* retry next action */ }
  }, [])
  useEffect(() => { load() }, [load])

  const createStory = async () => {
    if (!newTitle.trim()) return
    setCreating(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/rpg/stories`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle.trim(), mystery: newMystery, use_common_lore: newCommonLore }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      setNewTitle(''); setNewMystery(false); setNewCommonLore(true)
      await load()
      setManageSlug(data.slug); setView('manage')
    } catch (e) {
      setError(e.message || 'Create failed')
    } finally {
      setCreating(false)
    }
  }

  const deleteStory = async (s) => {
    if (!window.confirm(`Delete "${s.title}"? The thread history and lore stay in the shared stores; the story config is removed.`)) return
    await fetch(`${API_BASE}/rpg/stories/${s.slug}`, { method: 'DELETE' })
    load()
  }

  if (view === 'manage' && manageSlug) {
    return <ManageStory slug={manageSlug} onBack={() => { setView('list'); setManageSlug(null); load() }} />
  }
  if (view === 'play' && playSlug) {
    return <PlayStory slug={playSlug} onBack={() => { rpgReader.stop(); setView('list'); setPlaySlug(null); load() }} />
  }
  if (view === 'read' && readSlug) {
    return <ViewStory slug={readSlug} onBack={() => { setView('list'); setReadSlug(null) }} />
  }

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6">
      <div className="max-w-5xl mx-auto space-y-6">
        {/* Create */}
        <div className="rounded-2xl border border-purple-700/40 bg-purple-950/20 p-4">
          <h2 className="text-lg font-semibold text-purple-200 mb-2">🎲 New Story</h2>
          <div className="flex items-center gap-3 flex-wrap">
            <input
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && createStory()}
              placeholder="Story title (e.g. The Clockwork Manor)"
              className="flex-1 min-w-[200px] rounded-xl bg-slate-800 border border-slate-700 px-3 py-2 text-slate-100"
            />
            <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer" title="Mystery format arms the secret-keeping Director (S3) so the plot can't be spoiled">
              <input type="checkbox" checked={newMystery} onChange={(e) => setNewMystery(e.target.checked)} className="accent-purple-500" />
              🕵 Mystery format
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer" title="Also search the shared Common lore docs (uploaded once, below) — attach only NEW lore to this story">
              <input type="checkbox" checked={newCommonLore} onChange={(e) => setNewCommonLore(e.target.checked)} className="accent-indigo-500" />
              📚 Use common lore docs
            </label>
            <button
              type="button"
              onClick={createStory}
              disabled={creating || !newTitle.trim()}
              className="rounded-xl bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white px-4 py-2 font-medium"
            >
              {creating ? 'Creating…' : 'Create story'}
            </button>
          </div>
          {error && <p className="text-sm text-red-400 mt-2">{error}</p>}
          <p className="text-xs text-slate-500 mt-2">
            the agent-as-DM is still the agent — his memory and voice carry over; the campaign
            keeps its own thread, lore, and memory shelf so stories never bleed into chat.
          </p>
        </div>

        {/* Story list */}
        {stories.length === 0 ? (
          <div className="text-center py-16 text-slate-500">
            <p className="text-lg">No stories yet</p>
            <p className="text-sm mt-2">Create one above to start a campaign.</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {stories.map((s) => (
              <div key={s.slug} className="rounded-2xl border border-slate-700/60 bg-slate-800/60 p-4 flex flex-col gap-1.5">
                <div className="flex items-start justify-between gap-2">
                  <h3 className="font-semibold text-slate-100 leading-snug">{s.title}</h3>
                  {s.mystery ? (
                    <span className="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 border border-purple-500/40"
                      title={s.sealed_hash ? `Mystery sealed — ${s.sealed_hash}` : 'Mystery format — seal it in Manage to arm the Director'}>
                      {s.sealed_hash ? '🔒 sealed' : '🕵 mystery'}
                    </span>
                  ) : null}
                </div>
                <p className="text-xs text-slate-500">
                  {s.lore_count != null ? `${s.lore_count} lore doc${s.lore_count === 1 ? '' : 's'}` : 'lore: —'}
                  {' · '}last played {fmtDate(s.last_played)}
                </p>
                <div className="flex gap-2 mt-2">
                  <button
                    type="button"
                    onClick={() => { setPlaySlug(s.slug); setView('play') }}
                    className="text-xs rounded-lg bg-purple-600 hover:bg-purple-500 px-2 py-1 text-white font-medium"
                  >
                    ▶ Play
                  </button>
                  <button
                    type="button"
                    onClick={() => { setReadSlug(s.slug); setView('read') }}
                    className="text-xs rounded-lg border border-slate-600 px-2 py-1 text-slate-300 hover:bg-slate-700"
                    title="Read the whole campaign so far — view-only, with export"
                  >
                    📖 View
                  </button>
                  <button
                    type="button"
                    onClick={() => { setManageSlug(s.slug); setView('manage') }}
                    className="text-xs rounded-lg border border-slate-600 px-2 py-1 text-slate-300 hover:bg-slate-700"
                  >
                    Manage
                  </button>
                  <button
                    type="button"
                    onClick={() => deleteStory(s)}
                    className="text-xs rounded-lg border border-slate-700 px-2 py-1 text-slate-500 hover:text-red-400 hover:border-red-500/50"
                  >
                    Delete
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Common lore — shared docs, uploaded once, used by opted-in stories */}
        <CommonLore />
      </div>
    </div>
  )
}

// ── Common lore: the rpg-common shelf (upload once, reuse everywhere) ─────────
function CommonLore() {
  const [show, setShow] = useState(false)
  const [lore, setLore] = useState([])
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef(null)

  const load = useCallback(() => {
    fetch(`${API_BASE}/rpg/common-lore`).then((r) => r.json())
      .then((d) => setLore(d.lore || [])).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  const upload = async (file) => {
    if (!file) return
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`${API_BASE}/rpg/common-lore`, { method: 'POST', body: form })
      if (!res.ok) { const d = await res.json().catch(() => ({})); window.alert(d.detail || 'Upload failed') }
      load()
    } finally { setUploading(false) }
  }

  return (
    <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 p-4">
      <div className="flex items-center justify-between">
        <button type="button" onClick={() => setShow((v) => !v)} className="text-sm font-semibold text-slate-300 hover:text-slate-100">
          {show ? '▾' : '▸'} 📚 Common lore
          <span className="ml-2 text-[10px] font-normal px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-300">
            {lore.length} doc{lore.length === 1 ? '' : 's'}
          </span>
        </button>
        {show && (
          <>
            <input ref={fileRef} type="file" accept=".txt,.md,.pdf,.docx,.pptx" className="hidden"
              onChange={(e) => { upload(e.target.files?.[0]); e.target.value = '' }} />
            <button type="button" onClick={() => fileRef.current?.click()} disabled={uploading}
              className="text-xs rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-3 py-1">
              {uploading ? 'Uploading…' : '+ Upload common doc'}
            </button>
          </>
        )}
      </div>
      <p className="text-xs text-slate-500 mt-1">
        Docs you use in almost every story — upload them here ONCE. Stories with
        "Use common lore docs" checked search these alongside their own lore, so you
        only attach the new stuff per story.
      </p>
      {show && (
        lore.length === 0 ? (
          <p className="text-xs text-slate-500 mt-2">Nothing here yet — upload your recurring lore docs.</p>
        ) : (
          <ul className="space-y-1 mt-2">
            {lore.map((f, i) => (
              <li key={i} className="text-sm text-slate-300 flex items-center gap-2">
                <span className="text-slate-500">📄</span>{f.filename || 'document'}
              </li>
            ))}
          </ul>
        )
      )}
    </div>
  )
}

// ── View: the whole campaign so far, read-only + export ───────────────────────
function ViewStory({ slug, onBack }) {
  const [story, setStory] = useState(null)
  const [messages, setMessages] = useState(null) // null = loading
  const topRef = useRef(null)
  const endRef = useRef(null)

  useEffect(() => {
    fetch(`${API_BASE}/rpg/stories/${slug}`).then((r) => r.json()).then(setStory).catch(() => {})
    fetch(`${API_BASE}/rpg/stories/${slug}/messages?limit=100000`).then((r) => r.json())
      .then((d) => setMessages(d.messages || [])).catch(() => setMessages([]))
  }, [slug])

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="flex items-center gap-3 px-6 py-3 border-b border-slate-800 flex-wrap">
        <button type="button" onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">← Stories</button>
        <h2 className="text-base font-semibold text-slate-100">📖 {story?.title || slug}</h2>
        {story ? <span className="text-xs text-slate-500">Act {story.current_act} · {messages ? `${messages.length} turns` : '…'}</span> : null}
        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-slate-600/40 text-slate-400">view only</span>
        <div className="ml-auto flex items-center gap-2">
          <a href={`${API_BASE}/rpg/stories/${slug}/export?fmt=md`} download
            className="text-xs rounded-lg border border-slate-600 px-2 py-1 text-slate-300 hover:bg-slate-700">
            ⬇ .md
          </a>
          <a href={`${API_BASE}/rpg/stories/${slug}/export?fmt=txt`} download
            className="text-xs rounded-lg border border-slate-600 px-2 py-1 text-slate-300 hover:bg-slate-700">
            ⬇ .txt
          </a>
          <button type="button" onClick={() => endRef.current?.scrollIntoView({ behavior: 'smooth' })}
            className="text-xs rounded-lg border border-slate-700 px-2 py-1 text-slate-400 hover:text-slate-200" title="Jump to the latest turn">
            ⤓ latest
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="max-w-3xl mx-auto space-y-5">
          <div ref={topRef} />
          {messages === null && <p className="text-center text-slate-500 py-10">Loading the campaign…</p>}
          {messages?.length === 0 && <p className="text-center text-slate-500 py-10">Nothing played yet.</p>}
          {messages?.map((m, i) => (
            <div key={i}>
              {m.role === 'user' ? (
                <div className="flex justify-end">
                  <div className="max-w-[80%] rounded-2xl px-4 py-2.5 bg-emerald-600/70 text-white text-sm">
                    <p className="whitespace-pre-wrap break-words">{m.content}</p>
                  </div>
                </div>
              ) : (
                <div className="rounded-2xl px-5 py-4 bg-slate-800/60 border border-purple-700/20 text-slate-100">
                  <div className="prose-rpg text-[15px] [&_p]:my-2 [&_p]:leading-relaxed [&_em]:text-slate-400">
                    <ReactMarkdown components={{ p: ({ children }) => <p className="whitespace-pre-wrap break-words">{children}</p> }}>
                      {formatTagsForDisplay(m.content) ?? ''}
                    </ReactMarkdown>
                  </div>
                </div>
              )}
            </div>
          ))}
          <div ref={endRef} />
          {messages?.length > 6 && (
            <div className="text-center pb-4">
              <button type="button" onClick={() => topRef.current?.scrollIntoView({ behavior: 'smooth' })}
                className="text-xs rounded-lg border border-slate-700 px-3 py-1.5 text-slate-400 hover:text-slate-200">
                ⤒ back to the beginning
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── S7: DM engine selector — identity vs engine ───────────────────────────────
// The DM's identity (addendum, instructions, memory bank) is model-agnostic;
// this picks which LLM performs it. Kimi = the agent's stack (default, local path);
// everything else routes via OpenRouter. Writer and Director are independent.
function EngineSection({ story, patch }) {
  const [data, setData] = useState(null)          // {engines, openrouter_key_set, privacy_note}
  const [show, setShow] = useState(false)
  const [keyInput, setKeyInput] = useState('')
  const [keySaving, setKeySaving] = useState(false)
  const [showKeyEntry, setShowKeyEntry] = useState(false)
  const [overlay, setOverlay] = useState(null)    // {engine, text}
  const [overlaySaving, setOverlaySaving] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [verifyResults, setVerifyResults] = useState(null)

  const load = useCallback(() => {
    fetch(`${API_BASE}/rpg/engines`).then((r) => r.json()).then(setData).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  const writerId = story.writer_engine || 'kimi'
  const directorId = story.director_engine || 'kimi'
  const engines = data?.engines || []
  const writer = engines.find((e) => e.id === writerId)

  useEffect(() => {
    // Overlay editor follows the selected writer engine (kimi included — the
    // dialect layer exists for every engine, empty until play reveals a need).
    if (!show) { setOverlay(null); return }
    fetch(`${API_BASE}/rpg/engines/${writerId}/overlay`).then((r) => r.json())
      .then(setOverlay).catch(() => setOverlay(null))
  }, [writerId, show])

  const saveKey = async () => {
    if (!keyInput.trim()) return
    setKeySaving(true)
    try {
      const r = await fetch(`${API_BASE}/rpg/engines/key`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: keyInput.trim() }),
      })
      if (r.ok) { setKeyInput(''); setShowKeyEntry(false); load() }
      else { const d = await r.json().catch(() => ({})); window.alert(d.detail || 'Key save failed') }
    } finally { setKeySaving(false) }
  }

  const saveOverlay = async () => {
    if (!overlay) return
    setOverlaySaving(true)
    try {
      await fetch(`${API_BASE}/rpg/engines/${writerId}/overlay`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: overlay.text }),
      })
    } finally { setOverlaySaving(false) }
  }

  const runVerify = async () => {
    setVerifying(true); setVerifyResults(null)
    try {
      const r = await fetch(`${API_BASE}/rpg/engines/verify`, { method: 'POST' })
      const d = await r.json().catch(() => ({}))
      setVerifyResults(d.results || [{ id: 'error', ok: false, error: d.detail || 'verify failed' }])
    } finally { setVerifying(false) }
  }

  const engineOption = (e) => `${e.label}${e.available ? '' : ' (unavailable)'}`
  const selectCls = 'rounded-lg bg-slate-900 border border-slate-700 px-2 py-1.5 text-sm text-slate-200 min-w-[220px]'

  return (
    <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 p-4">
      <div className="flex items-center justify-between">
        <button type="button" onClick={() => setShow((v) => !v)} className="text-sm font-semibold text-slate-300 hover:text-slate-100">
          {show ? '▾' : '▸'} DM engine
          <span className="ml-2 text-[10px] font-normal px-1.5 py-0.5 rounded bg-slate-600/40 text-slate-400">
            {writerId}{story.mystery ? ` / dir: ${directorId}` : ''}
          </span>
        </button>
        {show && (
          <button type="button" onClick={runVerify} disabled={verifying}
            className="text-xs rounded-lg border border-slate-600 px-2 py-1 text-slate-300 hover:bg-slate-700 disabled:opacity-50"
            title="Calls every OpenRouter engine twice: completion + latency + cache-hit check. Takes a minute.">
            {verifying ? 'Testing engines…' : '🧪 Test engines'}
          </button>
        )}
      </div>
      <p className="text-xs text-slate-500 mt-1">
        Same DM, different voice-actor: the identity (addendum, instructions, memory) stays the agent's;
        this picks which model narrates. Changing mid-campaign is fine — prose voice may shift, but
        memory and continuity persist. {data?.privacy_note ? <span className="text-slate-600">{data.privacy_note}</span> : null}
      </p>
      {show && data && (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap gap-4">
            <label className="text-xs text-slate-400 space-y-1 block">
              <span className="block">Writer (the narrating DM)</span>
              <select value={writerId} onChange={(e) => patch({ writer_engine: e.target.value })} className={selectCls}>
                {engines.map((e) => <option key={e.id} value={e.id}>{engineOption(e)}</option>)}
              </select>
            </label>
            {story.mystery ? (
              <label className="text-xs text-slate-400 space-y-1 block">
                <span className="block">Director (secret-keeper — independent)</span>
                <select value={directorId} onChange={(e) => patch({ director_engine: e.target.value })} className={selectCls}>
                  {engines.map((e) => <option key={e.id} value={e.id}>{engineOption(e)}</option>)}
                </select>
              </label>
            ) : null}
          </div>
          {writer && (
            <div className="text-xs text-slate-400 space-y-1">
              <p>{writer.description} <span className="text-slate-600">· context {Math.round((writer.context_window || 0) / 1000)}k · ${writer.price_in}/M in, ${writer.price_out}/M out · caching: {writer.caching}</span></p>
              {!writer.good_at_rules && (
                <p className="text-amber-300/90">⚠ weaker at rules/state adjudication — mind it if this story leans on dice or mechanics.</p>
              )}
              {writer.price_step && (
                <p className="text-amber-300/90">⚠ {writer.price_step.note || `price steps up beyond ${writer.price_step.tokens} tokens`} — the play view warns when a turn crosses it.</p>
              )}
              {!writer.available && (
                <p className="text-red-300">✗ unavailable: {writer.unavailable_reason} — turns fall back to Kimi until fixed.</p>
              )}
            </div>
          )}
          {/* OpenRouter key (stored server-side in .env; effective immediately) */}
          <div className="flex items-center gap-2 text-xs">
            <span className={data.openrouter_key_set ? 'text-emerald-300' : 'text-amber-300'}>
              {data.openrouter_key_set ? '🔑 OpenRouter key set' : '🔑 OpenRouter key missing (non-Kimi engines need it)'}
            </span>
            {showKeyEntry ? (
              <span className="flex items-center gap-2">
                <input type="password" value={keyInput} onChange={(e) => setKeyInput(e.target.value)}
                  placeholder="sk-or-v1-…" autoComplete="off"
                  className="rounded-lg bg-slate-900 border border-slate-700 px-2 py-1 text-xs text-slate-200 w-64" />
                <button type="button" onClick={saveKey} disabled={keySaving || !keyInput.trim()}
                  className="rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-2 py-1">
                  {keySaving ? 'Saving…' : 'Save'}
                </button>
                <button type="button" onClick={() => { setShowKeyEntry(false); setKeyInput('') }}
                  className="text-slate-500 hover:text-slate-300">cancel</button>
              </span>
            ) : (
              <button type="button" onClick={() => setShowKeyEntry(true)}
                className="rounded-lg border border-slate-600 px-2 py-0.5 text-slate-400 hover:text-slate-200 hover:border-slate-500">
                {data.openrouter_key_set ? 'replace' : 'enter key'}
              </button>
            )}
          </div>
          {/* Per-engine overlay (the dialect layer) */}
          {overlay && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-semibold text-slate-400">Engine overlay — {writer?.label || writerId} <span className="font-normal text-slate-600">(dialect lines injected only when this engine narrates; empty until play reveals a need)</span></span>
                <button type="button" onClick={saveOverlay} disabled={overlaySaving}
                  className="text-xs rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-3 py-1">
                  {overlaySaving ? 'Saving…' : 'Save'}
                </button>
              </div>
              <textarea value={overlay.text} onChange={(e) => setOverlay({ ...overlay, text: e.target.value })}
                rows={5} spellCheck={false}
                className="w-full rounded-lg bg-slate-900/70 border border-slate-700 px-3 py-2 text-xs text-slate-200 font-mono" />
            </div>
          )}
          {/* Verify results table */}
          {verifyResults && (
            <div className="overflow-x-auto">
              <table className="text-xs text-slate-300 w-full">
                <thead>
                  <tr className="text-slate-500 text-left">
                    <th className="pr-3 py-1">engine</th><th className="pr-3">ok</th><th className="pr-3">latency</th>
                    <th className="pr-3">cache hit</th><th className="pr-3">$/turn uncached</th><th className="pr-3">$/turn cached</th><th>note</th>
                  </tr>
                </thead>
                <tbody>
                  {verifyResults.map((r) => (
                    <tr key={r.id} className="border-t border-slate-700/50">
                      <td className="pr-3 py-1 font-mono">{r.id}</td>
                      <td className="pr-3">{r.ok ? '✓' : '✗'}</td>
                      <td className="pr-3">{r.latency_s != null ? `${r.latency_s}s / ${r.latency2_s}s` : '—'}</td>
                      <td className="pr-3">{r.cache_hit === true ? `✓ ${r.cached_tokens} tok` : r.cache_hit === false ? '✗ none' : '—'}</td>
                      <td className="pr-3">{r.turn_cost_uncached != null ? `$${r.turn_cost_uncached}` : '—'}</td>
                      <td className="pr-3">{r.turn_cost_cached != null ? `$${r.turn_cost_cached}` : '—'}</td>
                      <td className="text-slate-500">{r.error || r.sample || ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Manage one story: instructions, lore, mystery/goalie, addendum view ───────
function ManageStory({ slug, onBack }) {
  const [story, setStory] = useState(null)
  const [instr, setInstr] = useState('')
  const [lore, setLore] = useState([])
  const [addendum, setAddendum] = useState('')
  const [addendumCustom, setAddendumCustom] = useState(false)
  const [addendumSaving, setAddendumSaving] = useState(false)
  const [showAddendum, setShowAddendum] = useState(false)
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [seal, setSeal] = useState(null)      // {sealed, hash, versions}
  const [seed, setSeed] = useState('')
  const [sealing, setSealing] = useState(false)
  const [reveal, setReveal] = useState(null)   // decrypted doc (post break-the-seal)
  const fileRef = useRef(null)

  const loadAll = useCallback(async () => {
    const s = await (await fetch(`${API_BASE}/rpg/stories/${slug}`)).json()
    setStory(s); setInstr(s.instructions || '')
    const l = await (await fetch(`${API_BASE}/rpg/stories/${slug}/lore`)).json()
    setLore(l.lore || [])
    fetch(`${API_BASE}/rpg/stories/${slug}/seal`).then((r) => r.json()).then(setSeal).catch(() => {})
  }, [slug])

  const doSeal = async () => {
    if (!window.confirm('Seal this mystery? The Director will author the full hidden truth, encrypt it, and lock in a hash. You will not see it — that is the point. Re-sealing keeps a version history.')) return
    setSealing(true)
    try {
      const r = await fetch(`${API_BASE}/rpg/stories/${slug}/seal`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ seed }),
      })
      const d = await r.json()
      if (!r.ok) { window.alert(d.detail || 'Seal failed'); return }
      await loadAll()
    } finally { setSealing(false) }
  }

  const breakSeal = async () => {
    if (!window.confirm('Break the seal and reveal the secrets document? Do this only after the story is over — it spoils everything.')) return
    const r = await fetch(`${API_BASE}/rpg/stories/${slug}/reveal`, { method: 'POST' })
    const d = await r.json()
    if (!r.ok) { window.alert(d.detail || 'Reveal failed'); return }
    setReveal(d)
  }
  useEffect(() => { loadAll() }, [loadAll])
  useEffect(() => {
    fetch(`${API_BASE}/rpg/addendum`).then((r) => r.json()).then((d) => {
      setAddendum(d.text); setAddendumCustom(d.is_custom)
    }).catch(() => {})
  }, [])

  const saveAddendum = async () => {
    setAddendumSaving(true)
    try {
      const r = await fetch(`${API_BASE}/rpg/addendum`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: addendum }),
      })
      const d = await r.json()
      setAddendumCustom(d.is_custom)
    } finally { setAddendumSaving(false) }
  }

  const revertAddendum = async () => {
    if (!window.confirm('Revert the base DM addendum to the built-in default? Your edits will be discarded.')) return
    const d = await (await fetch(`${API_BASE}/rpg/addendum/revert`, { method: 'POST' })).json()
    setAddendum(d.text); setAddendumCustom(false)
  }

  const saveInstr = async () => {
    setSaving(true)
    await fetch(`${API_BASE}/rpg/stories/${slug}/instructions`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions: instr }),
    })
    setSaving(false)
  }

  const patch = async (body) => {
    await fetch(`${API_BASE}/rpg/stories/${slug}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    loadAll()
  }

  const uploadLore = async (file) => {
    if (!file) return
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`${API_BASE}/rpg/stories/${slug}/lore`, { method: 'POST', body: form })
      if (!res.ok) { const d = await res.json().catch(() => ({})); window.alert(d.detail || 'Upload failed') }
      await loadAll()
    } finally { setUploading(false) }
  }

  if (!story) return <div className="flex-1 p-6 text-slate-500">Loading…</div>

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6">
      <div className="max-w-4xl mx-auto space-y-5">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div>
            <button type="button" onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">← Stories</button>
            <h2 className="text-lg font-semibold text-slate-100">{story.title}</h2>
            <p className="text-xs text-slate-500">thread {story.thread_id} · slug {slug}</p>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-sm text-slate-300 cursor-pointer" title="Also search the shared Common lore docs (RPG tab front page) alongside this story's own lore">
              <input type="checkbox" checked={!!story.use_common_lore} onChange={(e) => patch({ use_common_lore: e.target.checked })} className="accent-indigo-500" />
              📚 Common lore
            </label>
            <label className="flex items-center gap-1.5 text-sm text-slate-300 cursor-pointer">
              <input type="checkbox" checked={!!story.mystery} onChange={(e) => patch({ mystery: e.target.checked })} className="accent-purple-500" />
              🕵 Mystery
            </label>
            {story.mystery ? (
              <label className="flex items-center gap-1.5 text-sm text-slate-300 cursor-pointer" title="Director goalie: checks the Writer's draft against hidden truth">
                <input type="checkbox" checked={!!story.goalie} onChange={(e) => patch({ goalie: e.target.checked })} className="accent-purple-500" />
                🥅 Goalie
              </label>
            ) : null}
            {story.mystery ? (
              <label className="flex items-center gap-1.5 text-sm text-slate-300 cursor-pointer" title="Airtight (highest-stakes, default off): the Writer works only from recent prose, no long-range pattern accumulation. Costs continuity.">
                <input type="checkbox" checked={!!story.airtight} onChange={(e) => patch({ airtight: e.target.checked })} className="accent-purple-500" />
                🛡 Airtight
              </label>
            ) : null}
          </div>
        </div>

        {/* Mystery integrity: seal ceremony + break-the-seal (S4) */}
        {story.mystery && (
          <div className="rounded-2xl border border-purple-700/40 bg-purple-950/20 p-4">
            <h3 className="text-sm font-semibold text-purple-200 mb-2">🔒 Mystery integrity</h3>
            {seal?.sealed ? (
              <div className="space-y-2">
                <p className="text-sm text-slate-300">
                  Sealed — <code className="text-purple-300 text-xs">{seal.hash}</code>
                  {seal.versions > 1 ? <span className="text-slate-500"> · {seal.versions} versions</span> : null}
                </p>
                <p className="text-xs text-slate-500">
                  The secrets are fixed and encrypted. This hash was committed at seal time — after the story,
                  break the seal to verify the mystery was set from turn one.
                </p>
                <div className="flex gap-2">
                  <button type="button" onClick={doSeal} disabled={sealing}
                    className="text-xs rounded-lg border border-purple-600 px-2 py-1 text-purple-300 hover:bg-purple-600/20">
                    {sealing ? 'Re-sealing…' : 'Re-seal (new version)'}
                  </button>
                  <button type="button" onClick={breakSeal}
                    className="text-xs rounded-lg border border-amber-600/60 px-2 py-1 text-amber-300 hover:bg-amber-600/20">
                    Break the seal (reveal)
                  </button>
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-slate-500">
                  Give the Director a seed premise (a sentence or two of the mystery's shape). It will author
                  the full hidden truth — NPCs' souls vs masks, the 3-Act arc, reveal conditions — encrypt it,
                  and commit a hash. Neither you nor the agent-the-Writer will see it.
                </p>
                <textarea value={seed} onChange={(e) => setSeed(e.target.value)} rows={3}
                  placeholder="Seed the mystery (optional — leave blank to let the Director invent one from your lore)."
                  className="w-full rounded-xl bg-slate-900 border border-slate-700 px-3 py-2 text-sm text-slate-200" />
                <button type="button" onClick={doSeal} disabled={sealing}
                  className="rounded-xl bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white px-4 py-2 text-sm font-medium">
                  {sealing ? 'Sealing…' : '🔒 Seal the mystery'}
                </button>
              </div>
            )}
            {reveal && (
              <div className="mt-3 rounded-lg bg-slate-900/80 border border-amber-700/40 p-3">
                <p className="text-xs text-amber-300 mb-1">Revealed — sha256 {reveal.hash?.slice(0, 16)}… (matches the sealed hash above)</p>
                <pre className="text-xs text-slate-300 whitespace-pre-wrap max-h-[50vh] overflow-y-auto">{reveal.doc}</pre>
              </div>
            )}
          </div>
        )}

        {/* S7: engine selector */}
        <EngineSection story={story} patch={patch} />

        {/* Instructions */}
        <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 p-4">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-semibold text-slate-300">Story instructions</h3>
            <button type="button" onClick={saveInstr} disabled={saving}
              className="text-xs rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-3 py-1">
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
          <p className="text-xs text-slate-500 mb-2">
            These instructions are your story's <strong>always-injected spine</strong> — put the world
            bible and your 3-Act outline here (in Known mode). They ride every turn in full, so the agent
            never loses the thread. Deep reference material (long lore, tables, NPC dossiers) belongs in
            the <strong>lore library</strong> below, which the RAG pulls from on demand. Spine = always
            present; lore = retrieved when relevant.
          </p>
          <textarea
            value={instr}
            onChange={(e) => setInstr(e.target.value)}
            rows={8}
            className="w-full rounded-xl bg-slate-900 border border-slate-700 px-3 py-2 text-sm text-slate-200 font-mono"
            placeholder="World bible + 3-Act outline + tone/canon. Injected in full every turn (the spine). Deep references go in lore."
          />
          <div className="flex items-center gap-2 mt-2">
            <span className="text-xs text-slate-400">Current act:</span>
            {[1, 2, 3].map((a) => (
              <button key={a} type="button"
                onClick={() => fetch(`${API_BASE}/rpg/stories/${slug}/act?act=${a}`, { method: 'POST' }).then(loadAll)}
                className={`text-xs rounded px-2 py-0.5 border ${story.current_act === a ? 'bg-purple-600 border-purple-500 text-white' : 'border-slate-600 text-slate-400 hover:bg-slate-700'}`}>
                Act {a}
              </button>
            ))}
            <span className="text-[11px] text-slate-600">(rides retention as act:{story.current_act}; in mystery mode the Director owns the arc)</span>
          </div>
        </div>

        {/* Lore */}
        <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 p-4">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-semibold text-slate-300">Lore library <span className="text-slate-500 font-normal">(project rpg-{slug}, DM-private)</span></h3>
            <input ref={fileRef} type="file" accept=".txt,.md,.pdf,.docx,.pptx" className="hidden"
              onChange={(e) => { uploadLore(e.target.files?.[0]); e.target.value = '' }} />
            <button type="button" onClick={() => fileRef.current?.click()} disabled={uploading}
              className="text-xs rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-3 py-1">
              {uploading ? 'Uploading…' : '+ Upload lore doc'}
            </button>
          </div>
          {lore.length === 0 ? (
            <p className="text-xs text-slate-500">No lore yet — upload PDFs, TXT, DOCX, MD. the agent pulls from these while running this story only.</p>
          ) : (
            <ul className="space-y-1">
              {lore.map((f, i) => (
                <li key={i} className="text-sm text-slate-300 flex items-center gap-2">
                  <span className="text-slate-500">📄</span>{f.filename || 'document'}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Base DM addendum — editable, applies to ALL stories */}
        <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 p-4">
          <div className="flex items-center justify-between">
            <button type="button" onClick={() => setShowAddendum((v) => !v)} className="text-sm font-semibold text-slate-300 hover:text-slate-100">
              {showAddendum ? '▾' : '▸'} Base DM addendum
              <span className={`ml-2 text-[10px] font-normal px-1.5 py-0.5 rounded ${addendumCustom ? 'bg-emerald-500/20 text-emerald-300' : 'bg-slate-600/40 text-slate-400'}`}>
                {addendumCustom ? 'custom' : 'default'}
              </span>
            </button>
            {showAddendum && (
              <div className="flex gap-2">
                <button type="button" onClick={revertAddendum}
                  className="text-xs rounded-lg border border-slate-600 px-2 py-1 text-slate-400 hover:text-amber-300 hover:border-amber-500/50">
                  Revert to default
                </button>
                <button type="button" onClick={saveAddendum} disabled={addendumSaving}
                  className="text-xs rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-3 py-1">
                  {addendumSaving ? 'Saving…' : 'Save'}
                </button>
              </div>
            )}
          </div>
          <p className="text-xs text-slate-500 mt-1">
            The DM's base voice for EVERY story and every engine — your house style (the laws, the engine,
            the format contract). Edits apply to all stories; a per-story bible goes in the instructions
            above, and model-specific dialect lines go in the engine overlay.
          </p>
          {showAddendum && (
            <textarea
              value={addendum}
              onChange={(e) => setAddendum(e.target.value)}
              rows={18}
              className="mt-2 w-full rounded-lg bg-slate-900/70 border border-slate-700 px-3 py-2 text-xs text-slate-200 font-mono leading-relaxed"
              spellCheck={false}
            />
          )}
        </div>
      </div>
    </div>
  )
}

// ── Play view (S2/S3): themed chat with the DM, narration + Director state ────
function PlayStory({ slug, onBack }) {
  const [story, setStory] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [autoRead, setAutoRead] = useState(false)
  const [, setTick] = useState(0)
  const endRef = useRef(null)

  useEffect(() => rpgReader.onChange(() => setTick((t) => t + 1)), [])
  useEffect(() => {
    fetch(`${API_BASE}/rpg/stories/${slug}`).then((r) => r.json()).then(setStory).catch(() => {})
    fetch(`${API_BASE}/rpg/stories/${slug}/messages`).then((r) => r.json())
      .then((d) => setMessages(d.messages || [])).catch(() => {})
  }, [slug])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, loading])

  const send = async () => {
    const text = input.trim()
    if (!text || loading) return
    if (autoRead) rpgReader.unlock()
    setInput('')
    setLoading(true)
    setMessages((m) => [...m, { role: 'user', content: text }])
    try {
      const res = await fetch(`${API_BASE}/rpg/stories/${slug}/play`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      setMessages((m) => [...m, { role: 'assistant', content: data.response }])
      if (data.warnings?.length) {
        // S7: context-window / price-line / engine-fallback notes from the route
        setMessages((m) => [...m, { role: 'assistant', content: null, warning: data.warnings.join(' · ') }])
      }
      if (autoRead && data.response) rpgReader.read(data.response, { stripChoices: true }) // choice list read with eyes, not voice
    } catch (e) {
      setMessages((m) => [...m, { role: 'assistant', content: null, error: e.message || 'DM error' }])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-6 py-3 border-b border-slate-800 flex-wrap">
        <button type="button" onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">← Stories</button>
        <h2 className="text-base font-semibold text-slate-100">{story?.title || slug}</h2>
        {story?.mystery ? (
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 border border-purple-500/40">🕵 mystery</span>
        ) : null}
        {story ? <span className="text-xs text-slate-500">Act {story.current_act}</span> : null}
        <button type="button" onClick={() => { rpgReader.unlock(); setAutoRead((v) => !v) }}
          className={`ml-auto rounded-lg border px-2 py-1 text-xs ${autoRead ? 'bg-sky-600/20 border-sky-500 text-sky-300' : 'bg-slate-800 border-slate-700 text-slate-400'}`}
          title="Narrate the DM's prose aloud (the choice list is skipped by the voice)">
          🔊 narrate {autoRead ? 'ON' : 'OFF'}
        </button>
        {rpgReader.playing && (
          <button type="button" onClick={() => rpgReader.stop()} className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-300">⏹</button>
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
        {messages.length === 0 && !loading && (
          <p className="text-center text-slate-500 py-10">The story awaits. Set the scene, state your first action, and the DM will begin.</p>
        )}
        {messages.map((m, i) => (
          m.warning ? (
            <div key={i} className="flex justify-center">
              <div className="rounded-lg px-3 py-1.5 bg-amber-900/20 border border-amber-700/40 text-xs text-amber-300/90">
                ⚠ {m.warning}
              </div>
            </div>
          ) : (
          <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[85%] rounded-2xl px-4 py-3 ${
              m.role === 'user'
                ? 'bg-emerald-600/80 text-white'
                : m.error ? 'bg-red-900/40 text-red-200 border border-red-800/50'
                : 'bg-slate-800/80 text-slate-100 border border-purple-700/30'
            }`}>
              {m.role === 'assistant' && !m.error ? (
                <div className="prose-rpg [&_p]:my-2 [&_p]:leading-relaxed [&_em]:text-slate-400">
                  <ReactMarkdown components={{ p: ({ children }) => <p className="whitespace-pre-wrap break-words">{children}</p> }}>
                    {formatTagsForDisplay(m.content) ?? ''}
                  </ReactMarkdown>
                </div>
              ) : (
                <p className="whitespace-pre-wrap break-words">{m.content ?? m.error}</p>
              )}
            </div>
          </div>
          )
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="rounded-2xl px-4 py-3 bg-slate-800/80 border border-purple-700/30 text-sm text-slate-400 italic">
              {story?.mystery ? 'the Director is thinking…' : 'narrating…'}
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* Input */}
      <div className="border-t border-slate-800 px-6 py-4">
        <div className="flex gap-3 items-end max-w-4xl mx-auto">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            placeholder="What do you do?"
            rows={1}
            disabled={loading}
            className="flex-1 min-h-[44px] max-h-[160px] rounded-xl bg-slate-800/80 border border-slate-700 px-4 py-3 text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-purple-500/40 disabled:opacity-50"
          />
          <button type="button" onClick={send} disabled={loading || !input.trim()}
            className="rounded-xl bg-purple-600 hover:bg-purple-500 disabled:opacity-40 text-white px-5 py-3 font-medium">
            Act
          </button>
        </div>
      </div>
    </div>
  )
}
