// Books tab: import + chapter review + library (B1), player (B2).
import { useCallback, useEffect, useRef, useState } from 'react'
import { createBookPlayer } from '../../shared/bookPlayer.js'

const API_BASE = '/api'
const bookPlayer = createBookPlayer({ base: API_BASE })

function fmtDuration(s) {
  if (!s || s < 1) return null
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return h ? `${h}h ${m}m` : `${m}m`
}

export default function BooksTab({ reader = null }) {
  const [books, setBooks] = useState([])
  const [view, setView] = useState('library') // library | review | player
  const [reviewBook, setReviewBook] = useState(null) // full book with chapters
  const [playerBook, setPlayerBook] = useState(null)
  const [error, setError] = useState(null)
  const [importing, setImporting] = useState(false)
  const fileRef = useRef(null)

  const loadBooks = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/books`)
      if (res.ok) setBooks((await res.json()).books || [])
    } catch { /* retry on next action */ }
  }, [])

  useEffect(() => { loadBooks() }, [loadBooks])

  const openReview = useCallback(async (bookId) => {
    const res = await fetch(`${API_BASE}/books/${bookId}`)
    if (!res.ok) return
    setReviewBook(await res.json())
    setView('review')
  }, [])

  const handleImport = async (file) => {
    if (!file) return
    setError(null)
    setImporting(true)
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`${API_BASE}/books/import`, { method: 'POST', body: form })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
      await loadBooks()
      await openReview(data.id) // straight into chapter review
    } catch (e) {
      setError(e.message || 'Import failed')
    } finally {
      setImporting(false)
    }
  }

  const deleteBook = async (b) => {
    if (!window.confirm(`Delete "${b.title}" and its render cache? This can't be undone.`)) return
    await fetch(`${API_BASE}/books/${b.id}`, { method: 'DELETE' })
    loadBooks()
  }

  const openPlayer = useCallback(async (bookId) => {
    const res = await fetch(`${API_BASE}/books/${bookId}`)
    if (!res.ok) return
    setPlayerBook(await res.json())
    setView('player')
  }, [])

  if (view === 'review' && reviewBook) {
    return (
      <ReviewScreen
        book={reviewBook}
        onBack={() => { setView('library'); setReviewBook(null); loadBooks() }}
        onReload={() => openReview(reviewBook.id)}
      />
    )
  }

  if (view === 'player' && playerBook) {
    return (
      <PlayerScreen
        book={playerBook}
        books={books}
        reader={reader}
        onSwitchBook={(id) => openPlayer(id)}
        onBack={() => { bookPlayer.stop(); setView('library'); setPlayerBook(null); loadBooks() }}
      />
    )
  }

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6">
      <div className="max-w-5xl mx-auto space-y-6">
        {/* Import */}
        <div className="rounded-2xl border border-slate-700/60 bg-slate-800/50 p-4">
          <div className="flex items-center gap-3 flex-wrap">
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.epub"
              className="hidden"
              onChange={(e) => { handleImport(e.target.files?.[0]); e.target.value = '' }}
            />
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              disabled={importing}
              className="rounded-xl bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-4 py-2 font-medium"
            >
              {importing ? 'Importing…' : '📚 Import book (.txt / .epub)'}
            </button>
            <p className="text-sm text-slate-400">
              Public-domain or DRM-free files only (Project Gutenberg, Standard Ebooks,
              your DRM-free purchases). DRM-protected epubs are declined.
            </p>
          </div>
          {error && <p className="text-sm text-red-400 mt-2">{error}</p>}
        </div>

        {/* Library */}
        {books.length === 0 ? (
          <div className="text-center py-16 text-slate-500">
            <p className="text-lg">No books yet</p>
            <p className="text-sm mt-2">Import a .txt or .epub to start your library.</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {books.map((b) => (
              <div key={b.id} className="rounded-2xl border border-slate-700/60 bg-slate-800/60 p-4 flex flex-col gap-1.5">
                <div className="flex items-start justify-between gap-2">
                  <h3 className="font-semibold text-slate-100 leading-snug">{b.title}</h3>
                  {b.status === 'review' && (
                    <span className="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40">
                      review
                    </span>
                  )}
                </div>
                {b.author && <p className="text-sm text-slate-400">{b.author}</p>}
                <p className="text-xs text-slate-500">
                  {b.chapter_count} chapter{b.chapter_count === 1 ? '' : 's'}
                  {fmtDuration(b.audio_seconds) ? ` · ${fmtDuration(b.audio_seconds)} rendered` : ''}
                </p>
                <div className="flex gap-2 mt-2">
                  {b.status !== 'review' && (
                    <button
                      type="button"
                      onClick={() => openPlayer(b.id)}
                      className="text-xs rounded-lg bg-emerald-600/80 hover:bg-emerald-500 px-2 py-1 text-white font-medium"
                    >
                      ▶ Listen
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => openReview(b.id)}
                    className="text-xs rounded-lg border border-slate-600 px-2 py-1 text-slate-300 hover:bg-slate-700"
                  >
                    {b.status === 'review' ? 'Review chapters' : 'Chapters'}
                  </button>
                  {b.status !== 'review' && (
                    <button
                      type="button"
                      onClick={async () => {
                        await fetch(`${API_BASE}/books/${b.id}/prerender`, { method: 'POST' })
                        window.alert('Whole book queued — renders in the background at idle (yields to voice sessions and active chatting). Progress shows on this card.')
                      }}
                      className="text-xs rounded-lg border border-indigo-500/50 px-2 py-1 text-indigo-300 hover:bg-indigo-600/20"
                      title="Overnight mode: queue every chapter for background rendering"
                    >
                      🌙 Render book
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => deleteBook(b)}
                    className="text-xs rounded-lg border border-slate-700 px-2 py-1 text-slate-500 hover:text-red-400 hover:border-red-500/50"
                  >
                    Delete
                  </button>
                </div>
                <PrerenderProgress bookId={b.id} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// Rolling pre-render progress on the library card (polls only while active).
function PrerenderProgress({ bookId }) {
  const [st, setSt] = useState(null)
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const d = await (await fetch(`${API_BASE}/books/${bookId}/prerender`)).json()
        if (alive) setSt(d)
      } catch { /* quiet */ }
    }
    poll()
    const iv = setInterval(poll, 8000)
    return () => { alive = false; clearInterval(iv) }
  }, [bookId])
  if (!st || !st.total) return null
  const active = st.current
  return (
    <p className="text-[11px] text-indigo-300/90 mt-1">
      {st.done}/{st.total} chapters rendered
      {active ? ` — ch. ${active.chapter + 1}: ${active.done}/${active.total} ¶` : st.done < st.total ? ' — waiting for idle' : ' ✓'}
    </p>
  )
}

// ── Player (B2) ───────────────────────────────────────────────────────────────
function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''
}

function PlayerScreen({ book, books, reader, onSwitchBook, onBack }) {
  const [, setTick] = useState(0)
  const [chapter, setChapter] = useState(0)
  const [voices, setVoices] = useState([{ id: 'clone', label: 'Cloned voice' }])
  const [voice, setVoice] = useState(book.voice || 'clone')
  const [position, setPosition] = useState(null)
  const [bookmarks, setBookmarks] = useState([])
  const [highlights, setHighlights] = useState([])
  const [selection, setSelection] = useState('')
  const activeRef = useRef(null)

  useEffect(() => bookPlayer.onChange(() => setTick((t) => t + 1)), [])

  // Load voices, position, bookmarks, highlights
  useEffect(() => {
    fetch(`${API_BASE}/books/voices`).then((r) => r.json()).then((d) => setVoices(d.voices || [])).catch(() => {})
    fetch(`${API_BASE}/books/${book.id}/position`).then((r) => r.json()).then(setPosition).catch(() => {})
    reloadMarks()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [book.id])

  const reloadMarks = () => {
    fetch(`${API_BASE}/books/${book.id}/bookmarks`).then((r) => r.json()).then((d) => setBookmarks(d.bookmarks || [])).catch(() => {})
    fetch(`${API_BASE}/books/${book.id}/highlights`).then((r) => r.json()).then((d) => setHighlights(d.highlights || [])).catch(() => {})
  }

  const savePosition = (ch, unit) => {
    fetch(`${API_BASE}/books/${book.id}/position`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chapter: ch, unit }),
    }).catch(() => {})
  }

  // Player callbacks: position persistence + chapter auto-advance (sleep-aware)
  useEffect(() => {
    bookPlayer.onPosition = (ch, unit) => savePosition(ch, unit)
    bookPlayer.onChapterEnd = async (ch) => {
      if (bookPlayer.sleepUntil === 'chapter') { bookPlayer.setSleep(null); return }
      if (ch + 1 < book.chapters.length) {
        await playChapter(ch + 1, 0)
      }
    }
    return () => { bookPlayer.onPosition = null; bookPlayer.onChapterEnd = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [book.id, voice, book.chapters.length])

  // Never-overlap: chat reader speaking pauses the book; voice session pauses it.
  useEffect(() => {
    if (!reader) return undefined
    return reader.onChange(() => {
      if (reader.playing && bookPlayer.playing) bookPlayer.pause('reader')
    })
  }, [reader])
  useEffect(() => {
    const iv = setInterval(async () => {
      if (!bookPlayer.playing) return
      try {
        const { active } = await (await fetch(`${API_BASE}/books/voice-active`)).json()
        if (active) bookPlayer.pause('voice')
      } catch { /* keep playing */ }
    }, 3000)
    return () => clearInterval(iv)
  }, [])

  const playChapter = async (ch, unit = 0) => {
    reader?.stop()
    setChapter(ch)
    await bookPlayer.loadChapter(book.id, ch, { voice })
    savePosition(ch, unit)
    bookPlayer.playFrom(unit)
  }

  const continueFrom = async () => {
    const ch = position?.chapter ?? 0
    await playChapter(ch, position?.unit ?? 0)
  }

  // Auto-scroll the active paragraph into view
  useEffect(() => {
    activeRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [bookPlayer.unitIdx, bookPlayer.playing]) // eslint-disable-line react-hooks/exhaustive-deps

  const changeVoice = async (v) => {
    setVoice(v)
    fetch(`${API_BASE}/books/${book.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice: v }),
    }).catch(() => {})
    if (bookPlayer.units.length) {
      window.alert('Voice changed — cached renders for the old voice no longer apply; new paragraphs render in the new voice.')
      bookPlayer.voice = v
    }
  }

  const addBookmark = async () => {
    const name = window.prompt('Bookmark name (optional):') ?? ''
    await fetch(`${API_BASE}/books/${book.id}/bookmarks`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chapter, unit: bookPlayer.unitIdx, name }),
    })
    reloadMarks()
  }

  const saveHighlight = async () => {
    if (!selection.trim()) return
    await fetch(`${API_BASE}/books/${book.id}/highlights`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chapter, unit: bookPlayer.unitIdx, text: selection.trim() }),
    })
    setSelection('')
    window.getSelection()?.removeAllRanges()
    reloadMarks()
  }

  const units = bookPlayer.units

  return (
    <div className="flex-1 overflow-hidden flex flex-col px-6 py-4">
      <div className="max-w-6xl mx-auto w-full flex flex-col gap-3 h-full">
        {/* Header row: back, book switch, voice, continue */}
        <div className="flex items-center gap-2 flex-wrap">
          <button type="button" onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">← Library</button>
          <select
            value={book.id}
            onChange={(e) => onSwitchBook(e.target.value)}
            className="rounded-lg bg-slate-800 border border-slate-700 px-2 py-1 text-sm text-slate-200"
          >
            {books.filter((b) => b.status !== 'review').map((b) => (
              <option key={b.id} value={b.id}>{b.title}</option>
            ))}
          </select>
          <select
            value={voice}
            onChange={(e) => changeVoice(e.target.value)}
            className="rounded-lg bg-slate-800 border border-slate-700 px-2 py-1 text-sm text-slate-200"
            title="Narration voice (library grows at B5)"
          >
            {voices.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
          </select>
          <label className="flex items-center gap-1.5 text-xs text-slate-400 cursor-pointer"
            title="Character-voice theater (legacy): the narrator spontaneously casts character voices in dialogue. OFF = consistent the agent (default) — cooler delivery, voice-drift retakes.">
            <input type="checkbox" checked={!!book.theater}
              onChange={async (e) => {
                await fetch(`${API_BASE}/books/${book.id}/theater?on=${e.target.checked}`, { method: 'POST' })
                book.theater = e.target.checked ? 1 : 0
              }}
              className="accent-amber-500" />
            🎭 theater
          </label>
          {position?.updated_at && (
            <button
              type="button"
              onClick={continueFrom}
              className="rounded-lg bg-sky-600/80 hover:bg-sky-500 px-3 py-1 text-sm text-white"
              title={`Resume chapter ${(position.chapter ?? 0) + 1}, paragraph position ${position.unit + 1}`}
            >
              ▶ Continue — last listened {fmtDate(position.updated_at)}, ch. {(position.chapter ?? 0) + 1}
            </button>
          )}
          {bookPlayer.pausedReason === 'voice' && (
            <span className="text-xs text-amber-400">paused — voice session live</span>
          )}
          {bookPlayer.pausedReason === 'reader' && (
            <span className="text-xs text-amber-400">paused — a chat message is being read</span>
          )}
          {bookPlayer.loadingUnit && <span className="text-xs text-slate-500 animate-pulse">rendering…</span>}
          <button
            type="button"
            onClick={async () => {
              await fetch(`${API_BASE}/books/${book.id}/prerender?chapter=${chapter}`, { method: 'POST' })
            }}
            className="text-xs rounded-lg border border-indigo-500/50 px-2 py-1 text-indigo-300 hover:bg-indigo-600/20"
            title="Render this chapter ahead in the background"
          >
            ⚡ render ahead
          </button>
        </div>

        {/* Controls */}
        <div className="flex items-center gap-2 flex-wrap rounded-2xl border border-slate-700/60 bg-slate-800/50 px-3 py-2">
          <button type="button" onClick={() => { const c = chapter - 1; if (c >= 0) playChapter(c) }} title="Previous chapter" className="rounded-lg border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-700">⏮</button>
          <button type="button" onClick={() => bookPlayer.skipUnit(-1)} title="Back a paragraph" className="rounded-lg border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-700">⏪</button>
          <button
            type="button"
            onClick={() => {
              if (bookPlayer.playing) bookPlayer.pause()
              else if (bookPlayer.units.length) { reader?.stop(); bookPlayer.resume() }
              else continueFrom()
            }}
            className="rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-1.5 text-lg"
            title="Play / pause"
          >
            {bookPlayer.playing ? '⏸' : '▶'}
          </button>
          <button type="button" onClick={() => bookPlayer.skipUnit(1)} title="Forward a paragraph" className="rounded-lg border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-700">⏩</button>
          <button type="button" onClick={() => { const c = chapter + 1; if (c < book.chapters.length) playChapter(c) }} title="Next chapter" className="rounded-lg border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-700">⏭</button>
          {/* Scrubber: paragraph granularity within the chapter */}
          <input
            type="range"
            min={0}
            max={Math.max(0, units.length - 1)}
            value={bookPlayer.unitIdx}
            onChange={(e) => bookPlayer.playFrom(parseInt(e.target.value, 10))}
            className="flex-1 min-w-[120px] accent-emerald-400"
            title={`Paragraph ${bookPlayer.unitIdx + 1} / ${units.length}`}
          />
          <label className="text-xs text-slate-400 flex items-center gap-1">
            🔉<input type="range" min={0} max={1} step={0.05} value={bookPlayer.volume}
              onChange={(e) => bookPlayer.setVolume(parseFloat(e.target.value))}
              className="w-20 accent-sky-400" title="Volume (independent of system)" />
          </label>
          <select
            value={bookPlayer.rate}
            onChange={(e) => bookPlayer.setRate(parseFloat(e.target.value))}
            className="rounded bg-slate-800 border border-slate-700 px-1.5 py-0.5 text-xs text-slate-300"
            title="Playback speed (pitch preserved)"
          >
            {[0.75, 1, 1.25, 1.5, 1.75, 2].map((r) => <option key={r} value={r}>{r}×</option>)}
          </select>
          <button type="button" onClick={() => bookPlayer.reroll()} title="Re-roll this paragraph (fresh take, replaces the cached one)" className="rounded-lg border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-700">🎲</button>
          <button type="button" onClick={addBookmark} title="Drop a named bookmark here" className="rounded-lg border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-700">🔖</button>
          <select
            value={bookPlayer.sleepUntil === 'chapter' ? 'chapter' : bookPlayer.sleepUntil ? 'on' : ''}
            onChange={(e) => {
              const v = e.target.value
              bookPlayer.setSleep(v === 'chapter' ? 'chapter' : v ? parseInt(v, 10) : null)
            }}
            className="rounded bg-slate-800 border border-slate-700 px-1.5 py-0.5 text-xs text-slate-300"
            title="Sleep timer"
          >
            <option value="">😴 off</option>
            <option value="15">15 min</option>
            <option value="30">30 min</option>
            <option value="60">60 min</option>
            <option value="chapter">end of chapter</option>
          </select>
        </div>

        <div className="flex-1 grid grid-cols-1 lg:grid-cols-4 gap-3 min-h-0">
          {/* Chapter list */}
          <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 overflow-y-auto divide-y divide-slate-700/40">
            {book.chapters.map((ch) => (
              <button
                key={ch.idx}
                type="button"
                onClick={() => playChapter(ch.idx)}
                className={`w-full text-left px-3 py-1.5 text-xs truncate ${
                  ch.idx === chapter ? 'bg-emerald-600/20 text-emerald-200' : 'text-slate-300 hover:bg-slate-700/40'
                }`}
                title={ch.title}
              >
                {ch.idx + 1}. {ch.title}
              </button>
            ))}
          </div>

          {/* Follow-along text pane */}
          <div
            className="lg:col-span-2 rounded-2xl border border-slate-700/60 bg-slate-800/40 overflow-y-auto p-4 space-y-3"
            onMouseUp={() => setSelection(window.getSelection()?.toString() || '')}
          >
            {selection.trim() && (
              <div className="sticky top-0 z-10 flex justify-end">
                <button type="button" onClick={saveHighlight}
                  className="rounded-lg bg-amber-500/90 hover:bg-amber-400 text-slate-900 text-xs font-semibold px-2 py-1 shadow">
                  ★ Save highlight
                </button>
              </div>
            )}
            {units.length === 0 ? (
              <p className="text-sm text-slate-500">Pick a chapter (or hit ▶ Continue) — the text follows the narration here.</p>
            ) : units.map((u) => (
              u.pause ? (
                <p key={u.unit} className="text-center text-slate-600">· · ·</p>
              ) : (
                <p
                  key={u.unit}
                  ref={u.unit === bookPlayer.unitIdx ? activeRef : null}
                  onDoubleClick={() => { savePosition(chapter, u.unit); bookPlayer.playFrom(u.unit) }}
                  className={`text-[15px] leading-relaxed cursor-pointer rounded-lg px-2 py-1 transition-colors ${
                    u.unit === bookPlayer.unitIdx
                      ? 'bg-emerald-500/15 text-slate-100 border border-emerald-500/30'
                      : 'text-slate-300 hover:bg-slate-700/30'
                  }`}
                  title={u.flagged
                    ? 'This paragraph failed transcript verification after retakes — 🎲 re-roll it while playing'
                    : 'Double-click to play from this paragraph'}
                >
                  {u.flagged && <span className="text-amber-400 mr-1" title="Verification failed — worth a re-roll">⚠</span>}
                  {u.text}
                </p>
              )
            ))}
          </div>

          {/* Bookmarks + highlights */}
          <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 overflow-y-auto p-3 space-y-4">
            <div>
              <h4 className="text-xs font-semibold text-slate-400 mb-1.5">🔖 Bookmarks</h4>
              {bookmarks.length === 0 && <p className="text-xs text-slate-600">None yet — 🔖 drops one at the current paragraph.</p>}
              {bookmarks.map((bm) => (
                <div key={bm.id} className="flex items-center gap-1.5 text-xs py-0.5 group">
                  <button type="button" onClick={() => playChapter(bm.chapter, bm.unit)}
                    className="flex-1 text-left text-slate-300 hover:text-emerald-300 truncate">
                    {bm.name || `ch. ${bm.chapter + 1} ¶${bm.unit + 1}`}
                    <span className="text-slate-600"> · {fmtDate(bm.created_at)}</span>
                  </button>
                  <button type="button" onClick={async () => {
                    await fetch(`${API_BASE}/books/${book.id}/bookmarks/${bm.id}`, { method: 'DELETE' }); reloadMarks()
                  }} className="opacity-0 group-hover:opacity-100 text-slate-600 hover:text-red-400">×</button>
                </div>
              ))}
            </div>
            <div>
              <h4 className="text-xs font-semibold text-slate-400 mb-1.5">★ Highlights</h4>
              {highlights.length === 0 && <p className="text-xs text-slate-600">Select text in the reading pane to save one.</p>}
              {highlights.map((h) => (
                <div key={h.id} className="text-xs py-1 group border-b border-slate-700/30">
                  <p className="text-slate-300 italic">“{h.text.slice(0, 140)}{h.text.length > 140 ? '…' : ''}”</p>
                  <div className="flex gap-2 mt-0.5">
                    <span className="text-slate-600">ch. {h.chapter + 1} · {fmtDate(h.created_at)}</span>
                    <button type="button" onClick={() => playChapter(h.chapter, h.unit)}
                      className="text-emerald-400/80 hover:text-emerald-300">▶ play from here</button>
                    <button type="button" onClick={async () => {
                      await fetch(`${API_BASE}/books/${book.id}/highlights/${h.id}`, { method: 'DELETE' }); reloadMarks()
                    }} className="opacity-0 group-hover:opacity-100 text-slate-600 hover:text-red-400">×</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Chapter review: rename / merge-up / split ─────────────────────────────────
function ReviewScreen({ book, onBack, onReload }) {
  const [selected, setSelected] = useState(null) // chapter idx for split view
  const [paras, setParas] = useState([])
  const [renaming, setRenaming] = useState(null) // idx being renamed
  const [renameVal, setRenameVal] = useState('')
  const [busy, setBusy] = useState(false)

  const api = async (path, opts) => {
    setBusy(true)
    try {
      const res = await fetch(`${API_BASE}/books/${book.id}${path}`, opts)
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        window.alert(d.detail || `HTTP ${res.status}`)
        return false
      }
      return true
    } finally { setBusy(false) }
  }

  const openChapter = async (idx) => {
    const res = await fetch(`${API_BASE}/books/${book.id}/chapters/${idx}`)
    if (!res.ok) return
    setParas((await res.json()).paragraphs || [])
    setSelected(idx)
  }

  const rename = async (idx) => {
    if (await api(`/chapters/${idx}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: renameVal }),
    })) { setRenaming(null); onReload() }
  }

  const mergeUp = async (idx) => {
    if (await api(`/chapters/${idx}/merge_up`, { method: 'POST' })) { setSelected(null); onReload() }
  }

  const splitAt = async (paraIdx) => {
    if (await api(`/chapters/${selected}/split`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paragraph_index: paraIdx }),
    })) { setSelected(null); onReload() }
  }

  const markReady = async () => {
    if (await api('', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'ready' }),
    })) onBack()
  }

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6">
      <div className="max-w-5xl mx-auto space-y-4">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div>
            <button type="button" onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">← Library</button>
            <h2 className="text-lg font-semibold text-slate-100">{book.title}</h2>
            <p className="text-sm text-slate-400">{book.author} · {book.chapters.length} chapters — check the detection, then mark ready</p>
          </div>
          <button
            type="button"
            onClick={markReady}
            disabled={busy}
            className="rounded-xl bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-4 py-2 font-medium"
          >
            ✓ Chapters look right
          </button>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Chapter list */}
          <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 divide-y divide-slate-700/50 overflow-hidden">
            {book.chapters.map((ch) => (
              <div key={ch.idx} className={`px-3 py-2 flex items-center gap-2 ${selected === ch.idx ? 'bg-slate-700/40' : ''}`}>
                <span className="text-xs text-slate-500 w-7 shrink-0">{ch.idx + 1}</span>
                {renaming === ch.idx ? (
                  <span className="flex-1 flex gap-1">
                    <input
                      value={renameVal}
                      onChange={(e) => setRenameVal(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && rename(ch.idx)}
                      className="flex-1 text-sm rounded bg-slate-900 border border-slate-600 px-2 py-0.5 text-slate-100"
                      autoFocus
                    />
                    <button type="button" onClick={() => rename(ch.idx)} className="text-emerald-400 text-sm">✓</button>
                    <button type="button" onClick={() => setRenaming(null)} className="text-slate-500 text-sm">✕</button>
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => openChapter(ch.idx)}
                    className="flex-1 text-left text-sm text-slate-200 hover:text-white truncate"
                    title={`${ch.title} — ${ch.para_count} paragraphs, ${ch.char_count.toLocaleString()} chars`}
                  >
                    {ch.title || `Chapter ${ch.idx + 1}`}
                  </button>
                )}
                <span className="text-[10px] text-slate-600 shrink-0">{ch.para_count}¶</span>
                <button
                  type="button"
                  onClick={() => { setRenaming(ch.idx); setRenameVal(ch.title) }}
                  className="text-slate-500 hover:text-slate-300 text-xs shrink-0"
                  title="Rename"
                >
                  ✎
                </button>
                {ch.idx > 0 && (
                  <button
                    type="button"
                    onClick={() => mergeUp(ch.idx)}
                    disabled={busy}
                    className="text-slate-500 hover:text-amber-300 text-xs shrink-0"
                    title="Merge into the previous chapter"
                  >
                    ⤴
                  </button>
                )}
              </div>
            ))}
          </div>

          {/* Paragraphs of the selected chapter — click ✂ to split before one */}
          <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 p-3 max-h-[70vh] overflow-y-auto">
            {selected === null ? (
              <p className="text-sm text-slate-500">
                Select a chapter to preview its prepared text. Use ✂ on a paragraph to
                split the chapter before it, ⤴ in the list to merge a chapter upward,
                ✎ to rename.
              </p>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-slate-500 mb-1">
                  {book.chapters.find((c) => c.idx === selected)?.title} — prepared text (what the voice will read)
                </p>
                {paras.map((p, i) => (
                  <div key={i} className="group/para flex gap-2 items-start">
                    {i > 0 ? (
                      <button
                        type="button"
                        onClick={() => splitAt(i)}
                        disabled={busy}
                        className="opacity-0 group-hover/para:opacity-100 text-xs text-slate-500 hover:text-amber-300 shrink-0 mt-0.5"
                        title="Split chapter before this paragraph"
                      >
                        ✂
                      </button>
                    ) : <span className="w-4 shrink-0" />}
                    <p className={`text-sm whitespace-pre-wrap ${p === '[[scene-break]]' ? 'text-slate-600 italic' : 'text-slate-300'}`}>
                      {p === '[[scene-break]]' ? '· · ·  (scene break — rendered as a pause)' : p}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
