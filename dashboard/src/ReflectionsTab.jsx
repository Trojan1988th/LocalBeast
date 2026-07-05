// Reflections: a quiet, dated shared journal. The user writes an entry
// (optionally naming what they read); the agent writes its own note beneath.
// Deliberately unadorned: no streaks, no stats, no gamification. Presence,
// not pressure.
import { useCallback, useEffect, useState } from 'react'

const API_BASE = '/api'

function fmtDate(iso) {
  return new Date(iso + 'T12:00:00').toLocaleDateString(undefined,
    { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
}

export default function ReflectionsTab() {
  const [entries, setEntries] = useState(null)
  const [status, setStatus] = useState(null)
  const [passage, setPassage] = useState('')
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)
  const [pendingNote, setPendingNote] = useState(false)

  const load = useCallback(async () => {
    try {
      const d = await (await fetch(`${API_BASE}/reflections`)).json()
      setEntries(d.entries || [])
      setStatus(d.status || null)
    } catch { /* quiet */ }
  }, [])
  useEffect(() => { load() }, [load])

  const save = async () => {
    if (!text.trim() || saving) return
    setSaving(true)
    setPendingNote(true)
    try {
      const res = await fetch(`${API_BASE}/reflections`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ passage: passage.trim(), text: text.trim() }),
      })
      if (res.ok) { setPassage(''); setText(''); await load() }
    } finally {
      setSaving(false)
      setPendingNote(false)
    }
  }

  const markRest = async () => {
    await fetch(`${API_BASE}/reflections/rest-day`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    load()
  }

  return (
    <div className="max-w-2xl mx-auto space-y-10 pb-16">
      {/* Composer — today */}
      <div className="pt-4">
        <p className="text-sm text-slate-500">
          {new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
          {status?.book_position ? <span className="text-slate-600"> · you're in {status.book_position.replace(/^Book \d+ /, '')}</span> : null}
        </p>
        <input
          value={passage}
          onChange={(e) => setPassage(e.target.value)}
          placeholder="What you read (optional — a chapter, a passage, an article)"
          className="mt-3 w-full bg-transparent border-0 border-b border-slate-800 focus:border-slate-600 focus:ring-0 focus:outline-none px-0 py-1.5 text-slate-300 placeholder-slate-600"
        />
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          placeholder="What you read, and what it said to you…"
          className="mt-3 w-full bg-transparent border-0 focus:ring-0 focus:outline-none px-0 py-1 text-slate-200 placeholder-slate-600 leading-relaxed resize-y"
        />
        <div className="flex items-center justify-between mt-2">
          <button type="button" onClick={markRest}
            className="text-xs text-slate-600 hover:text-slate-400"
            title="Mark today a rest day — the evening note stays quiet, no questions asked.">
            resting today
          </button>
          <button type="button" onClick={save} disabled={saving || !text.trim()}
            className="text-sm text-slate-300 hover:text-white disabled:opacity-40 border border-slate-700 hover:border-slate-500 rounded-lg px-4 py-1.5">
            {saving ? 'keeping it…' : 'keep this'}
          </button>
        </div>
        {pendingNote && (
          <p className="text-xs text-slate-500 mt-2 italic">the agent is reading what you wrote…</p>
        )}
      </div>

      {/* Entries */}
      {entries === null && <p className="text-slate-600 text-sm">…</p>}
      {entries?.length === 0 && (
        <p className="text-slate-600 text-sm">The first page is blank on purpose.</p>
      )}
      {entries?.map((e) => (
        <div key={e.id} className="border-t border-slate-800/70 pt-6">
          <p className="text-xs text-slate-500">
            {fmtDate(e.entry_date)}{e.passage ? <span className="text-slate-400"> · {e.passage}</span> : null}
          </p>
          <p className="mt-3 text-slate-200 leading-relaxed whitespace-pre-wrap">{e.user_text}</p>
          {e.agent_text && (
            <div className="mt-4 pl-4 border-l border-slate-700/60">
              <p className="text-slate-400 leading-relaxed whitespace-pre-wrap text-[15px]">{e.agent_text}</p>
              <p className="text-xs text-slate-600 mt-1.5">— the agent</p>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
