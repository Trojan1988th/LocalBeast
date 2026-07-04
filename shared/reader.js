// the agent Reader playback module (Phase B) — SHARED by the dashboard and the
// Electron overlay renderer (R4: one module, not a fork). No dependencies.
//
// Streams WAV from the render service (24kHz mono s16le) and plays it
// progressively via Web Audio, keeping the decoded PCM so replayLast() replays
// the EXACT take (take variance is real — reroll() is the "different take" path).
//
// Context-prepending (adopted at R3 review): read/readAuto(text, { context })
// sends the user's message alongside; the SERVICE decides whether/how to use it
// (READER_CONTEXT gate + length cap live server-side). Auto-read passes it;
// read-this / re-roll on historical messages stay context-free by not passing one.
//
// Long-message consent (post-R4): readAuto() caps the spoken portion at a
// natural paragraph break (~cap chars); the rest is PRE-RENDERED quietly after
// the head's render completes (the service is single-flight — a concurrent
// fetch would preempt the head render server-side, so we wait for fetch-done,
// which is well before playback-done at RTF<1). continueReading() then starts
// instantly from the buffer. Surfaces show a "continue reading" chip while
// `reader.continuation` is set; `reader.partial.fullRaw` identifies the message.

export const READER_BASE = 'http://127.0.0.1:5005'
export const READER_TAGS_RE = /\[(chuckle|laugh|sigh|gasp|groan|yawn|cough|sniffle)\]/gi
export const DEFAULT_AUTOREAD_CAP = 1500 // chars of speech text; 0 = no cap

// Strip <actions>...</actions> blocks the agent sometimes emits.
const ACTION_EMOJI = { heart: '❤️', smile: '😊', thumbsup: '👍', wave: '👋', star: '⭐' }
export function cleanActions(text) {
  if (!text || typeof text !== 'string') return text
  return text
    .replace(/<actions>\s*<react\s+emoji="(\w+)"\s*\/>\s*<\/actions>/gi,
      (_, name) => (ACTION_EMOJI[name?.toLowerCase()] ?? '') + ' ')
    .replace(/<actions>[\s\S]*?<\/actions>/gi, '')
    .trim()
}

// Display transform: show authored performance tags subtly (italic) in bubbles.
export function formatTagsForDisplay(text) {
  if (!text || typeof text !== 'string') return text
  return text.replace(READER_TAGS_RE, '*$&*')
}

// What the voice gets: the words + performance tags, minus markdown the engine
// would speak literally. Emoji + unknown tags are stripped service-side.
export function textForSpeech(text) {
  return (cleanActions(text) || '')
    .replace(/```[\s\S]*?```/g, ' Code block omitted. ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/(\*\*|__)(?=\S)([\s\S]*?\S)\1/g, '$2')
    .replace(/^\s*[-*+]\s+/gm, '')
    .trim()
}

// Split speech text at a natural break near cap: prefer the last paragraph
// break, fall back to the last sentence end, else hard cap.
export function splitAtBreak(text, cap) {
  if (!cap || text.length <= cap) return [text, null]
  const slice = text.slice(0, cap)
  let idx = slice.lastIndexOf('\n\n')
  if (idx < cap * 0.4) {
    let last = -1
    const re = /[.!?…]["')\]]?\s/g
    let m
    while ((m = re.exec(slice)) !== null) last = m.index + m[0].length
    idx = last > cap * 0.25 ? last : cap
  }
  const head = text.slice(0, idx).trim()
  const rest = text.slice(idx).trim()
  return rest ? [head, rest] : [text, null]
}

export function createReader({ base = READER_BASE } = {}) {
  return {
    ctx: null,
    abort: null,        // AbortController of the AUDIBLE fetch
    sources: [],
    cursor: 0,
    playing: false,
    last: null,         // { text, context, pcm } — exact-replay cache (speech text)
    partial: null,      // { fullRaw, head, rest, context, cap } — a capped auto-read
    continuation: null, // { text, context, pcm: [], done, consumed, ac }
    listeners: new Set(),
    onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn) },
    emit() { this.listeners.forEach((fn) => fn()) },
    // The autoplay-unlock gesture: must be called from a user click at least once.
    unlock() {
      if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)()
      if (this.ctx.state === 'suspended') this.ctx.resume()
    },
    _dropContinuation() {
      if (this.continuation) {
        this.continuation.ac.abort()
        this.continuation = null
      }
      this.partial = null
    },
    stop({ cancelServer = true, keepContinuation = false } = {}) {
      // Only bother the server when something is actually in flight — and keep
      // the promise so a follow-up read can AWAIT it. An un-awaited /cancel
      // racing the next /read kills the NEW render (measured 5/6 silent —
      // the "no audio at all" regression).
      const active = !!this.abort || this.playing ||
        !!(this.continuation && !this.continuation.done)
      if (this.abort) { this.abort.abort(); this.abort = null }
      if (!keepContinuation) this._dropContinuation()
      if (cancelServer && active) {
        this._cancelPending = fetch(`${base}/cancel`, { method: 'POST' })
          .catch(() => {})
          .finally(() => { this._cancelPending = null })
      }
      this.sources.forEach((s) => { try { s.stop() } catch { /* already ended */ } })
      this.sources = []
      this.playing = false
      this.emit()
    },
    async _awaitCancel() {
      if (this._cancelPending) { try { await this._cancelPending } catch { /* noop */ } }
    },
    _schedule(f32) {
      if (!this.ctx || !f32.length) return
      const buf = this.ctx.createBuffer(1, f32.length, 24000)
      buf.copyToChannel(f32, 0)
      const src = this.ctx.createBufferSource()
      src.buffer = buf
      src.connect(this.ctx.destination)
      const at = Math.max(this.cursor, this.ctx.currentTime + 0.08)
      src.start(at)
      this.cursor = at + buf.duration
      this.sources.push(src)
      src.onended = () => {
        this.sources = this.sources.filter((s) => s !== src)
        // A consumed continuation may still be streaming chunks — a momentary
        // source drain between chunks is not "finished".
        const contLive = this.continuation && this.continuation.consumed && !this.continuation.done
        if (!this.sources.length && !this.abort && !contLive) { this.playing = false; this.emit() }
      }
    },
    // POST /read and parse the WAV stream; onChunk(f32) per decoded chunk.
    // Returns the full PCM as one Float32Array. Throws on abort/error.
    async _fetchRender(text, context, signal, onChunk) {
      const body = { text }
      if (context) body.context = context
      const res = await fetch(`${base}/read`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal,
      })
      if (!res.ok || !res.body) throw new Error(`reader service ${res.status}`)
      const rd = res.body.getReader()
      const parts = []
      let skipped = 0
      let carry = new Uint8Array(0)
      for (;;) {
        const { done, value } = await rd.read()
        if (done) break
        let bytes = value
        if (skipped < 44) { // WAV header
          const take = Math.min(44 - skipped, bytes.length)
          bytes = bytes.subarray(take)
          skipped += take
        }
        if (!bytes.length) continue
        const merged = new Uint8Array(carry.length + bytes.length)
        merged.set(carry)
        merged.set(bytes, carry.length)
        const even = merged.length - (merged.length % 2) // s16 alignment across chunks
        carry = merged.subarray(even)
        if (!even) continue
        const i16 = new Int16Array(merged.buffer.slice(0, even))
        const f32 = Float32Array.from(i16, (v) => v / 32768)
        parts.push(f32)
        if (onChunk) onChunk(f32)
      }
      const total = parts.reduce((n, p) => n + p.length, 0)
      const pcm = new Float32Array(total)
      let off = 0
      for (const p of parts) { pcm.set(p, off); off += p.length }
      return pcm
    },
    // Play one text audibly. speechText must already be textForSpeech-clean when
    // preCleaned; context is passed through textForSpeech here either way.
    async _play(speechText, context) {
      const ac = new AbortController()
      this.abort = ac
      this.playing = true
      this.cursor = 0
      this.emit()
      try {
        const pcm = await this._fetchRender(
          speechText,
          context ? textForSpeech(context) : null,
          ac.signal,
          (f32) => this._schedule(f32),
        )
        this.last = { text: speechText, context, pcm }
        if (this.abort === ac) this.abort = null
        if (!this.sources.length) this.playing = false
        this.emit()
        return true
      } catch (e) {
        if (this.abort === ac) this.abort = null
        if (e.name !== 'AbortError') {
          this.playing = false
          this.emit()
          console.warn('reader:', e)
        }
        return false
      }
    },
    // Read-this: the WHOLE message, no cap, context only if explicitly given.
    async read(rawText, { context = null } = {}) {
      this.unlock()
      this.stop({ cancelServer: true })
      await this._awaitCancel() // cancel must land BEFORE the new /read registers
      const text = textForSpeech(rawText)
      if (!text) return
      await this._play(text, context)
    },
    // True while a capped auto-read has an unconsumed continuation — surfaces
    // key the "continue reading ▸" chip off this (+ partial.fullRaw to find
    // which message it belongs to).
    hasPendingContinuation() {
      return !!(this.continuation && !this.continuation.consumed)
    },
    // Auto-read: capped at a natural break; the rest pre-renders quietly after
    // the head's fetch completes and waits for continueReading().
    async readAuto(rawText, { context = null, cap = DEFAULT_AUTOREAD_CAP } = {}) {
      this.unlock()
      this.stop({ cancelServer: true })
      await this._awaitCancel() // cancel must land BEFORE the new /read registers
      const text = textForSpeech(rawText)
      if (!text) return
      const [head, rest] = splitAtBreak(text, cap)
      if (!rest) {
        await this._play(text, context)
        return
      }
      this.partial = { fullRaw: rawText, head, rest, context, cap }
      this.emit() // surfaces can show the chip immediately
      const ok = await this._play(head, context) // resolves at fetch-done (playback continues)
      if (!ok || this.partial?.rest !== rest) return // stopped/preempted meanwhile
      // Quiet pre-render of the continuation (service is free again: head fetch
      // is complete server-side; playback outlives it at RTF<1).
      const cont = {
        text: rest,
        context,
        pcm: [],
        done: false,
        consumed: false,
        failed: false,
        ac: new AbortController(),
      }
      this.continuation = cont
      this.emit()
      this._fetchRender(rest, context ? textForSpeech(context) : null, cont.ac.signal,
        (f32) => { cont.pcm.push(f32); if (cont.consumed) this._schedule(f32) })
        .then((pcm) => {
          cont.done = true
          cont.fullPcm = pcm
          if (cont.consumed) {
            this.last = { text: rest, context, pcm }
            if (this.continuation === cont) this.continuation = null
            if (!this.sources.length) this.playing = false
          }
          this.emit()
        })
        .catch(() => {
          // Aborted (stop/reroll dropped it) or render error. If the chip is
          // still up, keep it — the click falls back to a live render.
          cont.failed = true
        })
    },
    // Continue a capped auto-read: plays the buffered continuation instantly,
    // scheduling further chunks live if the pre-render is still streaming.
    continueReading() {
      const cont = this.continuation
      if (!cont || cont.consumed) return
      this.unlock()
      // Silence the head (it may still be playing) WITHOUT cancelling the
      // continuation's in-flight render.
      if (this.abort) { this.abort.abort(); this.abort = null }
      this.sources.forEach((s) => { try { s.stop() } catch { /* ended */ } })
      this.sources = []
      this.partial = null
      if (cont.failed && !cont.pcm.length) {
        // Pre-render never delivered — render live instead.
        this.continuation = null
        this._play(cont.text, cont.context)
        return
      }
      this.playing = true
      this.cursor = 0
      cont.consumed = true
      cont.pcm.forEach((f32) => this._schedule(f32))
      if (cont.done) {
        this.last = { text: cont.text, context: cont.context, pcm: cont.fullPcm }
        this.continuation = null
      }
      // If still streaming, this.continuation stays set (consumed) so stop()
      // can abort the live stream via _dropContinuation.
      this.emit()
    },
    replayLast() { // the exact same take, no re-render
      if (!this.last?.pcm?.length) return
      this.unlock()
      this.stop({ cancelServer: false, keepContinuation: true })
      this.playing = true
      this.cursor = 0
      this.emit()
      this._schedule(this.last.pcm)
    },
    // Re-roll = same text, fresh generation. On a capped auto-read this
    // re-rolls the portion currently on deck (head) and re-prefetches the rest.
    reroll() {
      if (this.partial) {
        const { fullRaw, context, cap } = this.partial
        this.readAuto(fullRaw, { context, cap })
      } else if (this.last) {
        this.read(this.last.text, { context: this.last.context })
      }
    },
  }
}
