// Book player (B2) — long-form audiobook playback over the books API.
//
// Architecture (BOOKS_RECON, approved): a queue of <audio> ELEMENTS over
// per-unit cached WAVs served by /api/books/... — NOT Web Audio, because
// <audio>.playbackRate preserves pitch (the audiobook staple) and decoded-PCM
// memory stays flat over hours. Streaming-first: an uncached unit's GET
// triggers the render server-side; we preload the next unit while the current
// one plays, so the pipeline stays ahead after the first unit.
//
// Never-overlap: pause() is the hook; the tab wires reader-activity and the
// voice-session flag (polled) to it. Scene-break units play as silence.

const PAUSE_MS = 1200 // scene-break beat

export function createBookPlayer({ base = '/api' } = {}) {
  return {
    bookId: null,
    voice: 'clone',
    chapter: 0,
    units: [],          // chapter's units: {unit, text, paras, pause}
    unitIdx: 0,
    playing: false,
    loadingUnit: false, // waiting on a (possibly live) render
    rate: 1,
    volume: 1,
    sleepUntil: null,   // ms epoch | 'chapter'
    pausedReason: null, // 'voice' | 'reader' | null
    audio: null,        // current HTMLAudioElement
    _preload: null,     // {idx, el}
    _pauseTimer: null,
    _gen: 0,            // generation token: invalidates stale async callbacks
    listeners: new Set(),
    onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn) },
    emit() { this.listeners.forEach((fn) => fn()) },

    _src(idx) {
      return `${base}/books/${this.bookId}/audio/${this.chapter}/${idx}?voice=${encodeURIComponent(this.voice)}`
    },

    async loadChapter(bookId, chapter, { voice } = {}) {
      this.stop()
      this.bookId = bookId
      if (voice) this.voice = voice
      this.chapter = chapter
      const res = await fetch(`${base}/books/${bookId}/chapters/${chapter}`)
      if (!res.ok) throw new Error(`chapter load ${res.status}`)
      this.units = (await res.json()).units || []
      this.unitIdx = 0
      this.emit()
    },

    _firstAudioUnit(from, dir = 1) {
      let i = from
      while (i >= 0 && i < this.units.length && this.units[i]?.pause) i += dir
      return i
    },

    async playFrom(unitIdx) {
      const gen = ++this._gen
      this._clearAudio()
      this.unitIdx = Math.max(0, Math.min(unitIdx, this.units.length - 1))
      this.playing = true
      this.pausedReason = null
      this.emit()
      await this._playCurrent(gen)
    },

    async _playCurrent(gen) {
      if (gen !== this._gen) return
      const u = this.units[this.unitIdx]
      if (!u) { this.playing = false; this.emit(); return }
      if (u.pause) {
        // Scene break: a beat of silence, then advance
        this._pauseTimer = setTimeout(() => {
          if (gen === this._gen && this.playing) this._advance(gen)
        }, PAUSE_MS / this.rate)
        this.emit()
        return
      }
      this.loadingUnit = true
      this.emit()
      const el = (this._preload?.idx === this.unitIdx) ? this._preload.el : new Audio(this._src(this.unitIdx))
      this._preload = null
      el.playbackRate = this.rate
      el.volume = this.volume
      this.audio = el
      el.onended = () => { if (gen === this._gen && this.playing) this._advance(gen) }
      el.onerror = () => {
        // Render preempted (live read/voice won the GPU) — back off and retry.
        if (gen !== this._gen) return
        setTimeout(() => {
          if (gen === this._gen && this.playing) {
            this.audio = new Audio(this._src(this.unitIdx))
            this._playCurrent(gen)
          }
        }, 4000)
      }
      el.oncanplay = () => { this.loadingUnit = false; this.emit() }
      try {
        await el.play()
      } catch { /* autoplay or abort — surfaced by state */ }
      this._preloadNext()
      this.emit()
    },

    _preloadNext() {
      const next = this._firstAudioUnit(this.unitIdx + 1)
      if (next < this.units.length && this._preload?.idx !== next) {
        const el = new Audio(this._src(next)) // GET starts the render server-side
        el.preload = 'auto'
        this._preload = { idx: next, el }
      }
    },

    _advance(gen) {
      // Sleep timer: end-of-chapter mode stops at the chapter boundary below;
      // minute mode is checked here between units (never mid-paragraph).
      if (typeof this.sleepUntil === 'number' && Date.now() >= this.sleepUntil) {
        this.sleepUntil = null
        this.pause()
        return
      }
      if (this.unitIdx + 1 >= this.units.length) {
        this.playing = false
        this.onChapterEnd?.(this.chapter)
        this.emit()
        return
      }
      this.unitIdx += 1
      this.onPosition?.(this.chapter, this.unitIdx)
      this._playCurrent(gen)
    },

    _clearAudio() {
      if (this._pauseTimer) { clearTimeout(this._pauseTimer); this._pauseTimer = null }
      if (this.audio) { this.audio.onended = null; this.audio.onerror = null; this.audio.pause(); this.audio = null }
    },

    pause(reason = null) {
      this._gen++
      if (this._pauseTimer) { clearTimeout(this._pauseTimer); this._pauseTimer = null }
      this.audio?.pause()
      this.playing = false
      this.pausedReason = reason
      this.emit()
    },

    resume() {
      if (!this.units.length) return
      this.playFrom(this.unitIdx)
    },

    stop() {
      this._gen++
      this._clearAudio()
      this._preload = null
      this.playing = false
      this.pausedReason = null
      this.emit()
    },

    skipUnit(dir) {
      const target = this._firstAudioUnit(this.unitIdx + dir, dir)
      if (target >= 0 && target < this.units.length) {
        this.onPosition?.(this.chapter, target)
        this.playFrom(target)
      }
    },

    setRate(r) {
      this.rate = r
      if (this.audio) this.audio.playbackRate = r
      this.emit()
    },

    setVolume(v) {
      this.volume = v
      if (this.audio) this.audio.volume = v
      this.emit()
    },

    setSleep(mode) { // null | minutes | 'chapter'
      this.sleepUntil = mode === 'chapter' ? 'chapter' : mode ? Date.now() + mode * 60_000 : null
      this.emit()
    },

    async reroll() { // fresh take for the CURRENT unit, replacing the cache
      const idx = this.unitIdx
      const wasPlaying = this.playing
      this.pause()
      this.loadingUnit = true
      this.emit()
      try {
        await fetch(`${base}/books/${this.bookId}/audio/${this.chapter}/${idx}/reroll?voice=${encodeURIComponent(this.voice)}`,
          { method: 'POST' })
      } catch { /* retryable */ }
      this.loadingUnit = false
      if (wasPlaying) this.playFrom(idx)
      else this.emit()
    },
  }
}
