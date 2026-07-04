import { useState, useRef, useEffect, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import { VoiceClient } from './voice'
import { createReader, formatTagsForDisplay, DEFAULT_AUTOREAD_CAP } from '../../shared/reader.js'

// ── the agent Reader (R4) — playback module shared with the dashboard ────────────
// Renderer-side Web Audio playback (READER_RECON's recommendation); voice_bot
// untouched. Serves the MAIN-thread typed surface only — the realtime pipeline
// owns the voice thread.
const reader = createReader()

const API_BASE = 'http://localhost:8000/api'
// O1 addendum: text chat can target either thread; voice turns ALWAYS go to
// "voice" (they flow through voice_bot, independent of this toggle).
//   voice — realtime the agent (default; snaps back on when voice connects)
//   main  — dashboard-context the agent
const DEFAULT_TEXT_THREAD = 'voice'
const THREAD_META = {
  voice: { label: 'VOICE thread (realtime the agent)', color: 'emerald' },
  main: { label: 'MAIN thread (dashboard the agent)', color: 'indigo' },
}

// ── Authenticated fetch (O0) ──────────────────────────────────────────────────
// the agent's API is Basic-auth gated (DASHBOARD_PASSWORD). The main process
// resolves the credential (launch env → overlay .env → the agent's .env) and hands
// us a ready-made header once at startup; every API call goes through apiFetch.
let AUTH_HEADER = null
function apiFetch(url, options = {}) {
  const headers = { ...(options.headers || {}) }
  if (AUTH_HEADER) headers['Authorization'] = AUTH_HEADER
  return fetch(url, { ...options, headers })
}
// How long to wait for the agent to respond before giving up (ms).
// Kimi-K2.5 with tool calls can take 60-90s on complex queries.
const CHAT_TIMEOUT_MS = 3 * 60 * 1000 // 3 minutes
// Vision can also be slow (60s+ on Chutes/Kimi) — give it the same budget.
const VISION_TIMEOUT_MS = 3 * 60 * 1000 // 3 minutes

// Strip <actions>...</actions> tags the agent sometimes emits
const EMOJI_MAP = { heart: '❤️', smile: '😊', thumbsup: '👍', wave: '👋', star: '⭐' }
function cleanContent(text) {
  if (!text || typeof text !== 'string') return text
  return text
    .replace(/<actions>\s*<react\s+emoji="(\w+)"\s*\/>\s*<\/actions>/gi,
      (_, name) => (EMOJI_MAP[name?.toLowerCase()] ?? '') + ' ')
    .replace(/<actions>[\s\S]*?<\/actions>/gi, '')
    .trim()
}

// ── Icons (inline SVG, no icon library needed) ────────────────────────────────
const Icon = {
  Minimize: () => (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
      <rect x="1" y="5.5" width="10" height="1.5" rx="0.75"/>
    </svg>
  ),
  Close: () => (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
      <path d="M1.5 1.5L10.5 10.5M10.5 1.5L1.5 10.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
    </svg>
  ),
  Send: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="2" x2="11" y2="13"/>
      <polygon points="22 2 15 22 11 13 2 9 22 2"/>
    </svg>
  ),
  Camera: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
      <circle cx="12" cy="13" r="4"/>
    </svg>
  ),
  Mic: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
      <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
      <line x1="12" y1="19" x2="12" y2="23"/>
      <line x1="8" y1="23" x2="16" y2="23"/>
    </svg>
  ),
  Eye: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/>
    </svg>
  ),
  EyeOff: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
      <line x1="1" y1="1" x2="23" y2="23"/>
    </svg>
  ),
  Trash: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6"/>
      <path d="M19 6l-1 14H6L5 6"/>
      <path d="M10 11v6M14 11v6"/>
      <path d="M9 6V4h6v2"/>
    </svg>
  ),
  Mouse: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5" y="2" width="14" height="20" rx="7"/>
      <line x1="12" y1="6" x2="12" y2="10"/>
    </svg>
  ),
}

// ── Input area (isolated state to avoid full-app re-renders on every keystroke) ─
function InputArea({
  onSend,
  loading,
  clickThrough,
  pendingScreenshot,
  onRemoveScreenshot,
  onScreenshot,
  screenshotLoading,
  error,
  isElectron,
  inputRef,
  textThread,
  onToggleThread,
  onPasteImages,
}) {
  const [input, setInput] = useState('')
  const textareaRef = useRef(null)

  // Ctrl+V image paste: clipboard images ALWAYS attach to the typed message
  // (never arm for voice — pasting into the text input is itself the intent).
  const handlePaste = useCallback((e) => {
    const files = [...(e.clipboardData?.items || [])]
      .filter((it) => it.kind === 'file' && it.type.startsWith('image/'))
      .map((it) => it.getAsFile())
      .filter(Boolean)
    if (!files.length) return // plain text paste proceeds normally
    e.preventDefault()
    Promise.all(files.map((f) => new Promise((resolve) => {
      const r = new FileReader()
      r.onload = (ev) => resolve(ev.target.result)
      r.onerror = () => resolve(null)
      r.readAsDataURL(f)
    }))).then((urls) => {
      const good = urls.filter(Boolean)
      if (good.length) onPasteImages(good)
    })
  }, [onPasteImages])

  const handleSend = useCallback(() => {
    const text = input.trim()
    if ((!text && !pendingScreenshot) || loading) return
    onSend(text || null, pendingScreenshot)
    setInput('')
  }, [input, pendingScreenshot, loading, onSend])

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div
      className="flex-shrink-0 px-2 pb-2 pt-1.5 no-drag"
      style={{ borderTop: '1px solid rgba(255,255,255,0.06)' }}
    >
      {/* Thread indicator — unmistakable text label, clickable to switch */}
      <button
        onClick={onToggleThread}
        className={`w-full mb-1 px-2 py-0.5 rounded text-[11px] font-semibold tracking-wide text-left transition-colors ${
          textThread === 'voice'
            ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 hover:bg-emerald-500/25'
            : 'bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 hover:bg-indigo-500/25'
        }`}
        title="Click to switch which thread text messages go to (voice turns always go to the voice thread)"
      >
        text → {THREAD_META[textThread].label} · click to switch
      </button>
      {pendingScreenshot && (
        <div className="px-1 mb-1">
          <ScreenshotPreview
            dataUrl={Array.isArray(pendingScreenshot) ? pendingScreenshot[0] : pendingScreenshot}
            count={Array.isArray(pendingScreenshot) ? pendingScreenshot.length : 1}
            onRemove={onRemoveScreenshot}
          />
        </div>
      )}
      {error && <p className="text-xs text-red-400 px-1 mb-1">{error}</p>}
      <div className="flex gap-1.5 items-end">
        {isElectron && (
          <button
            onClick={onScreenshot}
            disabled={loading || screenshotLoading}
            className={`flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center transition-colors ${
              pendingScreenshot
                ? 'bg-emerald-600/40 text-emerald-300 border border-emerald-500/40'
                : 'text-slate-500 hover:text-slate-300 border border-transparent hover:border-white/10 hover:bg-white/5'
            } disabled:opacity-40 disabled:cursor-not-allowed`}
            title="Capture screenshot (Ctrl+Alt+S)"
          >
            {screenshotLoading ? (
              <span className="w-3 h-3 border border-slate-400 border-t-transparent rounded-full animate-spin" />
            ) : (
              <Icon.Camera />
            )}
          </button>
        )}
        <textarea
          ref={(el) => {
            textareaRef.current = el
            if (inputRef) inputRef.current = el
          }}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={clickThrough ? 'Click-through ON — toggle off to type' : 'Message the agent... (Enter to send)'}
          disabled={loading || clickThrough}
          rows={1}
          className="flex-1 rounded-lg px-3 py-2 text-xs resize-none focus:outline-none disabled:opacity-40 disabled:cursor-not-allowed"
          style={{
            background: 'rgba(255,255,255,0.06)',
            border: '1px solid rgba(255,255,255,0.1)',
            color: '#e2e8f0',
            minHeight: '32px',
            maxHeight: '80px',
          }}
          onInput={(e) => {
            e.target.style.height = 'auto'
            e.target.style.height = Math.min(e.target.scrollHeight, 80) + 'px'
          }}
        />
        <button
          onClick={handleSend}
          disabled={loading || (!input.trim() && !pendingScreenshot) || clickThrough}
          className="flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center bg-emerald-600 text-white hover:bg-emerald-500 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          title="Send (Enter)"
        >
          <Icon.Send />
        </button>
      </div>
    </div>
  )
}

// ── Armed-screenshot countdown (O3) ───────────────────────────────────────────
function ArmedCountdown({ until }) {
  const [left, setLeft] = useState(Math.max(0, Math.ceil((until - Date.now()) / 1000)))
  useEffect(() => {
    const t = setInterval(() => setLeft(Math.max(0, Math.ceil((until - Date.now()) / 1000))), 500)
    return () => clearInterval(t)
  }, [until])
  return (
    <span className="text-[11px] font-semibold text-amber-300 animate-pulse">
      📸 armed — speak now to show the agent ({left}s)
    </span>
  )
}

// ── Screenshot preview thumbnail ──────────────────────────────────────────────
function ScreenshotPreview({ dataUrl, count = 1, onRemove }) {
  return (
    <div className="relative inline-block mt-1 mb-1">
      <img
        src={dataUrl}
        alt="Screenshot preview"
        className="h-16 rounded border border-white/20 object-cover"
        style={{ maxWidth: '120px' }}
      />
      {count > 1 && (
        <span className="absolute bottom-0.5 right-0.5 text-[9px] px-1 rounded bg-black/70 text-slate-200">
          +{count - 1} detail
        </span>
      )}
      <button
        onClick={onRemove}
        className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-red-500 text-white flex items-center justify-center hover:bg-red-400 transition-colors no-drag"
        title="Remove screenshot"
      >
        <svg width="8" height="8" viewBox="0 0 12 12" fill="currentColor">
          <path d="M1.5 1.5L10.5 10.5M10.5 1.5L1.5 10.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
        </svg>
      </button>
    </div>
  )
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [messages, setMessages] = useState([])
  const [loading, setLoading] = useState(false)
  const [historyLoaded, setHistoryLoaded] = useState(false)
  const [error, setError] = useState(null)
  const [status, setStatus] = useState(null) // e.g. "Analyzing screenshot..." or "Waiting for the agent..."

  // Overlay controls
  const [opacity, setOpacity] = useState(0.92)
  const [clickThrough, setClickThrough] = useState(false)
  const [showControls, setShowControls] = useState(false)

  // Screenshot
  const [pendingScreenshot, setPendingScreenshot] = useState(null) // base64 dataURL
  const [screenshotLoading, setScreenshotLoading] = useState(false)

  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)
  const loadingRef = useRef(false)

  const isElectron = typeof window !== 'undefined' && !!window.electronAPI

  // ── API auth bootstrap ──────────────────────────────────────────────────────
  // Resolve the Basic auth header before the first API call (history load).
  const [authReady, setAuthReady] = useState(!(typeof window !== 'undefined' && !!window.electronAPI))
  useEffect(() => {
    if (!isElectron) return
    window.electronAPI
      .getApiAuth()
      .then((header) => {
        AUTH_HEADER = header
        if (!header) console.warn('[Overlay] no API credential found — requests may 401 (see main.js resolveApiPassword)')
      })
      .catch((err) => console.warn('[Overlay] auth bootstrap failed:', err))
      .finally(() => setAuthReady(true))
  }, [isElectron])

  // ── Text thread toggle (O1 addendum) ────────────────────────────────────────
  // Which thread TEXT messages target. Voice turns always go to "voice".
  const [textThread, setTextThread] = useState(DEFAULT_TEXT_THREAD)
  const textThreadRef = useRef(DEFAULT_TEXT_THREAD)
  textThreadRef.current = textThread

  // ── Reader (R4): auto-read toggle + defer-to-voice, persisted in settings ──
  const [readerAutoRead, setReaderAutoRead] = useState(false)
  const readerAutoReadRef = useRef(false)
  readerAutoReadRef.current = readerAutoRead
  const deferToVoiceRef = useRef(true) // readerDeferToVoice, default true
  const readerCapRef = useRef(DEFAULT_AUTOREAD_CAP) // readerAutoReadCap, 0 = no cap
  const [, setReaderTick] = useState(0)
  useEffect(() => reader.onChange(() => setReaderTick((t) => t + 1)), [])

  const toggleAutoRead = useCallback(() => {
    reader.unlock() // this click IS the autoplay-unlock gesture
    setReaderAutoRead((v) => {
      if (isElectron) window.electronAPI.setSettings({ readerAutoRead: !v }).catch(() => {})
      return !v
    })
  }, [isElectron])

  // Restore persisted choices from settings.json
  const [micDeviceId, setMicDeviceId] = useState('')
  const [micList, setMicList] = useState([])
  useEffect(() => {
    if (!isElectron) return
    window.electronAPI.getSettings().then((s) => {
      if (s?.textThread === 'voice' || s?.textThread === 'main') setTextThread(s.textThread)
      if (typeof s?.readerAutoRead === 'boolean') setReaderAutoRead(s.readerAutoRead)
      if (typeof s?.readerDeferToVoice === 'boolean') deferToVoiceRef.current = s.readerDeferToVoice
      if (Number.isFinite(s?.readerAutoReadCap)) readerCapRef.current = s.readerAutoReadCap
      if (s?.micDeviceId) {
        setMicDeviceId(s.micDeviceId)
        // Apply to the (lazily created) voice client so connect uses it
        getVoice().updateMic(s.micDeviceId)
      }
    }).catch(() => {})
  }, [isElectron])

  const refreshMicList = useCallback(() => {
    getVoice().getAllMics().then(setMicList).catch(() => {})
  }, [])

  const pickMic = useCallback((deviceId) => {
    setMicDeviceId(deviceId)
    getVoice().updateMic(deviceId)
    if (isElectron) window.electronAPI.setSettings({ micDeviceId: deviceId || null }).catch(() => {})
  }, [isElectron])

  const switchThread = useCallback((next) => {
    if (next !== 'main') reader.stop() // leaving the Reader's surface silences it
    setTextThread(next)
    setMessages([])          // clear view; history effect reloads from the new thread
    setHistoryLoaded(false)
    if (isElectron) window.electronAPI.setSettings({ textThread: next }).catch(() => {})
  }, [isElectron])

  const toggleThread = useCallback(() => {
    switchThread(textThreadRef.current === 'voice' ? 'main' : 'voice')
  }, [switchThread])

  // ── Voice (O1): connect/disconnect to voice_bot:8010 ───────────────────────
  // Disconnected by default at launch; toggled by button or Ctrl+Shift+V.
  const [voiceState, setVoiceState] = useState('off') // off|connecting|on|error
  const [voiceError, setVoiceError] = useState(null)
  const [userCaption, setUserCaption] = useState(null)
  const [botCaption, setBotCaption] = useState(null)
  const [botSpeaking, setBotSpeaking] = useState(false)
  const botSpeakingRef = useRef(false)
  botSpeakingRef.current = botSpeaking
  const voiceRef = useRef(null)
  const voiceStateRef = useRef('off')
  voiceStateRef.current = voiceState

  const getVoice = useCallback(() => {
    if (!voiceRef.current) {
      voiceRef.current = new VoiceClient({
        onState: (state, detail) => {
          // Never-overlap rule (R4): starting a voice session silences the
          // Reader immediately — the realtime pipeline owns the speakers.
          if (state === 'connecting' || state === 'on') reader.stop()
          setVoiceState(state)
          setVoiceError(state === 'error' ? detail : null)
          if (state === 'off' || state === 'error') {
            setUserCaption(null)
            setBotCaption(null)
            setBotSpeaking(false)
          }
          // Voice connected -> text defaults back to the voice thread so the
          // conversation is one stream. Manual toggle still works afterwards.
          if (state === 'on' && textThreadRef.current !== 'voice') {
            switchThread('voice')
          }
        },
        onUserCaption: (text, final) => {
          setUserCaption({ text, final })
          if (final) setBotCaption(null) // new turn — clear the old bot caption
        },
        onBotCaption: (text) => setBotCaption(text),
        onBotSpeaking: setBotSpeaking,
      })
    }
    return voiceRef.current
  }, [])

  const toggleVoice = useCallback(() => {
    const v = getVoice()
    if (v.connected || voiceState === 'connecting') v.disconnect()
    else v.connect()
  }, [getVoice, voiceState])

  // Voice hotkey from anywhere (Ctrl+Alt+V, registered in main.js)
  useEffect(() => {
    if (!isElectron) return
    const cleanup = window.electronAPI.onToggleVoice(() => toggleVoice())
    return cleanup
  }, [isElectron, toggleVoice])

  // Clean disconnect if the window unloads while connected
  useEffect(() => () => { voiceRef.current?.disconnect() }, [])

  // ── O2 push-to-talk + ACTUAL mic-state indicator ────────────────────────────
  // micOpen reflects the REAL local track state (polled every 250ms + updated
  // on every PTT edge), never the intended state. Any desync = release blocker,
  // so the poll is the source of truth and PTT events merely trigger it early.
  const [micOpen, setMicOpen] = useState(false)
  const micOpenRef = useRef(false)
  // What the mic SHOULD be (PTT held?). The truth poll enforces this: if the
  // media manager ever swaps in a fresh (enabled) track, the poll re-gates it
  // within 250ms — desired state always wins, and the display shows ACTUAL.
  const desiredMicRef = useRef(false)

  const refreshMicState = useCallback(() => {
    const v = voiceRef.current
    let actual = v?.isMicActuallyOpen() ?? false
    if (v?.connected && actual !== desiredMicRef.current) {
      v.setMicEnabled(desiredMicRef.current) // self-heal toward desired
      actual = v.isMicActuallyOpen()
    }
    if (actual !== micOpenRef.current) {
      micOpenRef.current = actual
      setMicOpen(actual)
      if (isElectron) window.electronAPI.reportMicOpen(actual) // opacity floor
    }
  }, [isElectron])

  // Truth poll — runs whenever voice is connected
  useEffect(() => {
    if (voiceState !== 'on') {
      refreshMicState()
      return
    }
    refreshMicList() // device labels become available once permission is granted
    const interval = setInterval(refreshMicState, 250)
    return () => clearInterval(interval)
  }, [voiceState, refreshMicState, refreshMicList])

  // PTT hold state from main (debounced there). Opening the mic while the agent is
  // speaking is deliberate barge-in — the pipeline handles it natively (VAD
  // fires on your speech and interrupts, same as the voice page).
  useEffect(() => {
    if (!isElectron) return
    const cleanup = window.electronAPI.onPttState((held) => {
      // Never-overlap rule (R4): the paddle always wins — a PTT press silences
      // any Reader playback before the mic opens.
      if (held) reader.stop()
      desiredMicRef.current = held
      if (held && botSpeakingRef.current) {
        // Paddle-priority barge-in: the press itself interrupts the agent —
        // explicit signal, no VAD dependency (the user's O2 design).
        voiceRef.current?.sendPttInterrupt()
      }
      voiceRef.current?.setMicEnabled(held)
      refreshMicState()
    })
    return cleanup
  }, [isElectron, refreshMicState])

  // SAFETY: main demands mic close (overlay hidden, etc.)
  useEffect(() => {
    if (!isElectron) return
    const cleanup = window.electronAPI.onForceMicClose(() => {
      desiredMicRef.current = false
      voiceRef.current?.setMicEnabled(false)
      refreshMicState()
    })
    return cleanup
  }, [isElectron, refreshMicState])

  // ── Scroll to bottom ────────────────────────────────────────────────────────
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  // ── Opacity sync to Electron ────────────────────────────────────────────────
  useEffect(() => {
    if (isElectron) window.electronAPI.setOpacity(opacity)
  }, [opacity, isElectron])

  // ── Click-through sync ──────────────────────────────────────────────────────
  useEffect(() => {
    if (isElectron) window.electronAPI.setClickThrough(clickThrough)
  }, [clickThrough, isElectron])

  // ── Listen for hotkey-triggered screenshot ──────────────────────────────────
  useEffect(() => {
    if (!isElectron) return
    const cleanup = window.electronAPI.onTriggerScreenshot(() => {
      handleScreenshot()
    })
    return cleanup
  }, [isElectron])

  // ── Listen for hotkey to toggle click-through (undo when stuck) ─────────────
  useEffect(() => {
    if (!isElectron) return
    const cleanup = window.electronAPI.onToggleClickThrough(() => {
      setClickThrough((v) => !v)
    })
    return cleanup
  }, [isElectron])

  // ── Load conversation history ────────────────────────────────────────────────
  const [loadError, setLoadError] = useState(null)
  const loadHistory = useCallback(async () => {
    const threadAtCall = textThread
    try {
      setLoadError(null)
      const res = await apiFetch(`${API_BASE}/messages?thread_id=${threadAtCall}&limit=200`)
      // Stale-response guard: if the user switched threads while this fetch
      // was in flight (10s poller), drop the result — otherwise old-thread
      // messages overwrite the freshly loaded view.
      if (textThreadRef.current !== threadAtCall) return
      if (!res.ok) {
        const text = await res.text()
        console.warn('[Overlay] messages fetch failed:', res.status, text.slice(0, 200))
        setLoadError(`API ${res.status} — is the agent running?`)
        return
      }
      const data = await res.json()
      if (textThreadRef.current !== threadAtCall) return
      setMessages(data.messages || [])
    } catch (err) {
      console.warn('[Overlay] messages fetch error:', err)
      setLoadError('Could not reach API — is the agent running?')
    } finally {
      if (textThreadRef.current === threadAtCall) setHistoryLoaded(true)
    }
  }, [textThread])

  useEffect(() => {
    if (!authReady) return // wait for the auth header before the first load
    loadHistory()
    const interval = setInterval(() => {
      if (!loadingRef.current) loadHistory()
    }, 10000)
    return () => clearInterval(interval)
  }, [loadHistory, authReady])

  // ── Screenshot capture (O3: dual-image; arms for voice or attaches to text) ──
  // armedChip: null | {state: 'armed'|'attached'|'expired', until?: number}
  const [armedChip, setArmedChip] = useState(null)

  // fromButton: clicking the camera button parks the cursor ON the overlay, so
  // the detail crop would always frame the overlay corner. Give the user a 3s
  // aiming window to move the cursor onto the thing she wants the agent to see.
  // The hotkey path stays instant (cursor is already where she's working).
  const [aimCountdown, setAimCountdown] = useState(0)

  const handleScreenshot = useCallback(async (fromButton = false) => {
    if (!isElectron || screenshotLoading) return
    setScreenshotLoading(true)
    try {
      if (fromButton) {
        for (let s = 3; s > 0; s--) {
          setAimCountdown(s)
          await new Promise((r) => setTimeout(r, 1000))
        }
        setAimCountdown(0)
      }
      const result = await window.electronAPI.captureScreenshot()
      if (!result?.images?.length) return
      console.log(`[Overlay] captured ${result.images.length} image(s) in ${result.tookMs}ms`)
      if (voiceStateRef.current === 'on' && textThreadRef.current === 'voice') {
        // Voice connected AND on the realtime surface: ARM for the next voice
        // turn. If the user has deliberately flipped text to MAIN, her intent is a
        // text attachment even while voice is up — don't hijack the capture.
        const res = await apiFetch(`${API_BASE}/voice/arm-image`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image_data_urls: result.images }),
        })
        if (res.ok) {
          const data = await res.json()
          setArmedChip({ state: 'armed', until: Date.now() + (data.ttl_s || 15) * 1000 })
        }
      } else {
        // Text mode: attach to the next typed message
        setPendingScreenshot(result.images)
        inputRef.current?.focus()
      }
    } catch (err) {
      console.error('Screenshot failed:', err)
    } finally {
      setScreenshotLoading(false)
    }
  }, [isElectron, screenshotLoading])

  // Track armed-image lifecycle: poll the server while armed so the chip flips
  // to attached (consumed by a voice turn) or expired — truth from the server.
  useEffect(() => {
    if (armedChip?.state !== 'armed') return
    const interval = setInterval(async () => {
      try {
        const res = await apiFetch(`${API_BASE}/voice/arm-image`)
        if (!res.ok) return
        const { state } = await res.json()
        if (state === 'consumed') {
          setArmedChip({ state: 'attached' })
          setTimeout(() => setArmedChip(null), 4000)
        } else if (state === 'none') {
          setArmedChip({ state: 'expired' })
          setTimeout(() => setArmedChip(null), 5000)
        }
      } catch { /* keep polling */ }
    }, 1500)
    return () => clearInterval(interval)
  }, [armedChip?.state])

  // ── Fetch with timeout helper ─────────────────────────────────────────────
  const fetchWithTimeout = useCallback((url, options, timeoutMs = CHAT_TIMEOUT_MS) => {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    return apiFetch(url, { ...options, signal: controller.signal })
      .finally(() => clearTimeout(timer))
  }, [])

  // ── Send message ─────────────────────────────────────────────────────────────
  const sendMessage = useCallback(async (text, screenshotData) => {
    if ((!text && !screenshotData) || loading) return
    // Gesture-callstack unlock (harmless in Electron, vital in a browser after
    // a reload with the auto-read toggle already ON).
    if (readerAutoReadRef.current) reader.unlock()

    const hasScreenshot = !!screenshotData
    const userPrompt = (text || '').trim() || 'I sent you a screenshot of my screen. What do you see?'

    setPendingScreenshot(null)
    setError(null)
    setStatus(null)
    loadingRef.current = true
    setLoading(true)

    // Optimistic user message shown immediately
    setMessages((prev) => [
      ...prev,
      {
        role: 'user',
        content: userPrompt + (hasScreenshot ? ' [screenshot]' : ''),
        metadata: { role_display: 'the user' },
        _optimistic: true,
      },
    ])

    try {
      // O3: images go DIRECTLY on /chat (Kimi K2.5 is natively multimodal —
      // verified 2026-07-03). The old two-model detour (/analyze-screenshot
      // describe → text chat) is retired; that endpoint remains available as
      // a documented fallback if the model ever loses vision.
      setStatus('Waiting for the agent...')
      const res = await fetchWithTimeout(
        `${API_BASE}/chat`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: userPrompt,
            thread_id: textThread,
            channel_type: 'overlay',
            image_data_urls: hasScreenshot ? screenshotData : null,
            // Reader (R4): the agent gets the read-aloud addendum when his reply
            // will be performed (main thread + auto-read on).
            read_aloud: textThread === 'main' && readerAutoReadRef.current,
          }),
        },
        CHAT_TIMEOUT_MS,
      )

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(err.detail || `HTTP ${res.status}`)
      }

      // Reader auto-read (R4): MAIN-thread typed surface only, and defers to a
      // live voice session (setting: readerDeferToVoice, default true). the user's
      // message rides along as delivery context (adopted at R3 review).
      const data = await res.json().catch(() => null)
      // Defer only to a LIVE session ('on'/'connecting') — a voice attempt that
      // ended in 'error' must not silently mute auto-read (root cause of the
      // "screenshot reply not read" report when voice had errored earlier).
      const voiceLive = voiceStateRef.current === 'on' || voiceStateRef.current === 'connecting'
      if (
        data?.response &&
        readerAutoReadRef.current &&
        textThreadRef.current === 'main' &&
        !(deferToVoiceRef.current && voiceLive)
      ) {
        reader.readAuto(data.response, { context: userPrompt, cap: readerCapRef.current })
      }

      // Reload history to get the real persisted messages (replaces the optimistic one)
      await loadHistory()
    } catch (err) {
      // AbortError means we hit the timeout — the agent may still be running.
      // Poll for the response rather than showing a hard error.
      if (err.name === 'AbortError') {
        setStatus(null)
        setError('Taking longer than expected — polling for response...')
        let attempts = 0
        const maxAttempts = 60
        const poll = setInterval(async () => {
          attempts++
          try {
            const res = await apiFetch(`${API_BASE}/messages?thread_id=${textThread}&limit=5`)
            if (res.ok) {
              const data = await res.json()
              const msgs = (data.messages || []).filter((m) => m.role !== 'tool')
              const lastMsg = msgs[msgs.length - 1]
              if (lastMsg?.role === 'assistant') {
                clearInterval(poll)
                setError(null)
                await loadHistory()
                loadingRef.current = false
                setLoading(false)
                return
              }
            }
          } catch { /* keep polling */ }
          if (attempts >= maxAttempts) {
            clearInterval(poll)
            setError('No response after 5 minutes. The agent may still be processing.')
            loadingRef.current = false
            setLoading(false)
          }
        }, 5000)
        return
      }

      setStatus(null)
      setError(err.message || 'Failed to send message')
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: null, metadata: {}, error: err.message || 'Something went wrong.' },
      ])
    } finally {
      setStatus(null)
      loadingRef.current = false
      setLoading(false)
    }
    }, [loading, loadHistory, fetchWithTimeout, textThread])

  const clearHistory = () => {
    setMessages([])
    setHistoryLoaded(false)
    setTimeout(loadHistory, 100)
  }

  const visibleMessages = messages.filter((m) => m.role !== 'tool')

  // ── Render ───────────────────────────────────────────────────────────────────
  // O2 mic indicator: window-edge glow driven by the ACTUAL mic state.
  // MIC OPEN (red, dominant) > AGENT SPEAKING (sky) > off. Also visible in
  // click-through mode; main enforces an opacity floor while the mic is open.
  const frameStyle = micOpen
    ? {
        border: '2px solid rgba(244,63,94,0.95)',
        boxShadow: '0 0 0 3px rgba(244,63,94,0.45), 0 0 28px rgba(244,63,94,0.6), 0 8px 32px rgba(0,0,0,0.6)',
      }
    : botSpeaking && voiceState === 'on'
      ? {
          border: '2px solid rgba(56,189,248,0.8)',
          boxShadow: '0 0 0 2px rgba(56,189,248,0.3), 0 0 20px rgba(56,189,248,0.4), 0 8px 32px rgba(0,0,0,0.6)',
        }
      : {
          border: '1px solid rgba(255,255,255,0.08)',
          boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
        }

  return (
    <div
      className="flex flex-col h-full rounded-xl overflow-hidden"
      style={{
        background: `rgba(15, 20, 30, ${Math.min(opacity, 0.97)})`,
        ...frameStyle,
      }}
    >
      {/* ── Title Bar (drag handle) ─────────────────────────────────────────── */}
      <div
        className="drag-region flex items-center justify-between px-3 py-2 flex-shrink-0"
        style={{ background: 'rgba(255,255,255,0.04)', borderBottom: '1px solid rgba(255,255,255,0.06)' }}
      >
        {/* Left: name + mic-state badge (reads ACTUAL track state) */}
        <div className="flex items-center gap-2 no-drag">
          <div className="w-2 h-2 rounded-full bg-emerald-400" title="Agent online" />
          <span className="text-xs font-semibold text-slate-200 tracking-wide">AGENT</span>
          {micOpen ? (
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-rose-500 text-white animate-pulse tracking-wider">
              ● MIC OPEN
            </span>
          ) : botSpeaking && voiceState === 'on' ? (
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-sky-500/80 text-white tracking-wider">
              AGENT SPEAKING
            </span>
          ) : voiceState === 'on' ? (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-600/60 text-slate-300 tracking-wider" title="Voice connected — hold F9 to talk">
              mic off · hold F9
            </span>
          ) : (
            <span className="text-xs text-slate-500">overlay</span>
          )}
        </div>

        {/* Right: controls toggle + window buttons */}
        <div className="flex items-center gap-1 no-drag">
          {/* Voice connect/disconnect (O1) — Ctrl+Shift+V */}
          {isElectron && (
            <button
              onClick={toggleVoice}
              className={`w-6 h-6 rounded flex items-center justify-center transition-colors ${
                voiceState === 'on'
                  ? 'text-emerald-400 bg-emerald-500/15'
                  : voiceState === 'connecting'
                    ? 'text-amber-400 animate-pulse'
                    : voiceState === 'error'
                      ? 'text-red-400'
                      : 'text-slate-500 hover:text-slate-300 hover:bg-white/10'
              }`}
              title={
                voiceState === 'on'
                  ? 'Voice connected — click to disconnect (Ctrl+Alt+V). Hold F9 to talk.'
                  : voiceState === 'connecting'
                    ? 'Connecting voice...'
                    : voiceState === 'error'
                      ? `Voice error: ${voiceError || 'unknown'} — click to retry`
                      : 'Connect voice (Ctrl+Alt+V)'
              }
            >
              <Icon.Mic />
            </button>
          )}

          {/* Settings toggle */}
          <button
            onClick={() => setShowControls((v) => !v)}
            className={`px-2 py-1 rounded text-xs transition-colors ${
              showControls ? 'bg-white/10 text-slate-200' : 'text-slate-500 hover:text-slate-300 hover:bg-white/5'
            }`}
            title="Toggle controls"
          >
            ⚙
          </button>

          {/* Minimize */}
          {isElectron && (
            <button
              onClick={() => window.electronAPI.minimize()}
              className="w-6 h-6 rounded flex items-center justify-center text-slate-500 hover:text-slate-300 hover:bg-white/10 transition-colors"
              title="Minimize"
            >
              <Icon.Minimize />
            </button>
          )}

          {/* Hide (Ctrl+Shift+R to bring back) */}
          {isElectron && (
            <button
              onClick={() => window.electronAPI.hide()}
              className="w-6 h-6 rounded flex items-center justify-center text-slate-500 hover:text-red-400 hover:bg-white/10 transition-colors"
              title="Hide overlay (Ctrl+Alt+R to show again — hiding closes the mic)"
            >
              <Icon.Close />
            </button>
          )}
        </div>
      </div>

      {/* ── Controls Panel ──────────────────────────────────────────────────── */}
      {showControls && (
        <div
          className="px-3 py-2.5 flex-shrink-0 space-y-2.5 no-drag"
          style={{ background: 'rgba(0,0,0,0.25)', borderBottom: '1px solid rgba(255,255,255,0.05)' }}
        >
          {/* Opacity slider */}
          <div className="flex items-center gap-2">
            <Icon.Eye />
            <span className="text-xs text-slate-400 w-14 flex-shrink-0">Opacity</span>
            <input
              type="range"
              min="0.2"
              max="1.0"
              step="0.05"
              value={opacity}
              onChange={(e) => setOpacity(parseFloat(e.target.value))}
              className="flex-1 h-1.5 accent-emerald-400 cursor-pointer"
              title={`Opacity: ${Math.round(opacity * 100)}%`}
            />
            <span className="text-xs text-slate-400 w-8 text-right">{Math.round(opacity * 100)}%</span>
          </div>

          {/* Click-through toggle */}
          {isElectron && (
            <div className="flex items-center gap-2">
              <Icon.Mouse />
              <span className="text-xs text-slate-400 flex-1">Click-through</span>
              <button
                onClick={() => setClickThrough((v) => !v)}
                className={`relative w-9 h-5 rounded-full transition-colors ${
                  clickThrough ? 'bg-amber-500' : 'bg-slate-600'
                }`}
                title={clickThrough ? 'Click-through ON — press Ctrl+Shift+C to turn off' : 'Click-through OFF (overlay is interactive)'}
              >
                <span
                  className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${
                    clickThrough ? 'translate-x-4' : 'translate-x-0.5'
                  }`}
                />
              </button>
              {clickThrough && (
                <span className="text-xs text-amber-400">WoW mode</span>
              )}
            </div>
          )}

          {/* Mic input device (O2 — OBS coexistence). Default = system default. */}
          <div className="flex items-center gap-2">
            <Icon.Mic />
            <span className="text-xs text-slate-400 w-14 flex-shrink-0">Mic</span>
            <select
              value={micDeviceId}
              onChange={(e) => pickMic(e.target.value)}
              onFocus={refreshMicList}
              className="flex-1 text-xs rounded px-1 py-0.5 bg-slate-800 text-slate-300 border border-white/10"
              title="Explicit input device — pick your headset mic so OBS and the agent don't fight over 'default'"
            >
              <option value="">System default</option>
              {micList.map((d) => (
                <option key={d.deviceId} value={d.deviceId}>
                  {d.label || `mic ${d.deviceId.slice(0, 8)}`}
                </option>
              ))}
            </select>
          </div>

          {/* Clear chat */}
          <div className="flex items-center gap-2">
            <Icon.Trash />
            <span className="text-xs text-slate-400 flex-1">Clear display</span>
            <button
              onClick={clearHistory}
              className="text-xs text-slate-500 hover:text-red-400 px-2 py-0.5 rounded border border-slate-700 hover:border-red-500/50 transition-colors"
            >
              Clear
            </button>
          </div>

          {/* Hotkeys reminder */}
          <div className="text-xs text-slate-600 space-y-0.5 pt-0.5 border-t border-white/5">
            <div><kbd className="text-slate-500">F9 (hold)</kbd> — push-to-talk (keyboard or mouse paddle)</div>
            <div><kbd className="text-slate-500">Ctrl+Alt+R</kbd> — show/hide overlay (hiding closes the mic)</div>
            <div><kbd className="text-slate-500">Ctrl+Alt+S</kbd> — screenshot + open</div>
            <div><kbd className="text-slate-500">Ctrl+Alt+C</kbd> — toggle click-through (undo when stuck)</div>
            <div><kbd className="text-slate-500">Ctrl+Alt+V</kbd> — connect/disconnect voice</div>
            <div className="text-slate-700">All bindings configurable in settings.json ("hotkeys")</div>
          </div>
        </div>
      )}

      {/* ── Messages ────────────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2 no-drag">
        {!historyLoaded ? (
          <div className="flex items-center justify-center h-full">
            <span className="text-xs text-slate-500">Connecting to the agent...</span>
          </div>
        ) : visibleMessages.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <div className="text-center px-4">
              {loadError ? (
                <>
                  <p className="text-xs text-amber-400">{loadError}</p>
                  <p className="text-xs text-slate-600 mt-1">Retrying every 10s…</p>
                </>
              ) : (
                <>
                  <p className="text-xs text-slate-500">No messages yet.</p>
                  <p className="text-xs text-slate-600 mt-1">Type below or press Ctrl+Shift+S to send a screenshot.</p>
                </>
              )}
            </div>
          </div>
        ) : null}

        {visibleMessages.map((msg, i) => {
          const isCron = msg.metadata?.role_display === 'cron'

          if (isCron) {
            return (
              <div key={i} className="flex justify-start">
                <div className="max-w-[90%]">
                  <p className="text-xs text-indigo-400 mb-0.5">⏰ Cron</p>
                  <div
                    className="rounded-xl px-3 py-2 text-xs"
                    style={{ background: 'rgba(99,102,241,0.15)', border: '1px solid rgba(99,102,241,0.25)', color: '#c7d2fe' }}
                  >
                    <p className="whitespace-pre-wrap break-words">{msg.content}</p>
                  </div>
                </div>
              </div>
            )
          }

          const isUser = msg.role === 'user'
          // Reader (R4): per-message read-this — MAIN-thread surface only.
          // Context-free by design (history reads don't reconstruct context).
          const readBtn = textThread === 'main' && msg.content ? (
            <button
              onClick={() => reader.read(msg.content)}
              className="self-end mb-0.5 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity text-slate-500 hover:text-emerald-400 text-[11px] no-drag"
              title="Read this aloud (fresh take each time)"
            >
              🔊
            </button>
          ) : null
          return (
            <div key={i} className={`flex gap-1 group ${isUser ? 'justify-end' : 'justify-start'}`}>
              {isUser && readBtn}
              <div
                className="max-w-[90%] rounded-xl px-3 py-2 text-xs"
                style={
                  isUser
                    ? { background: 'rgba(16,185,129,0.25)', border: '1px solid rgba(16,185,129,0.3)', color: '#d1fae5' }
                    : msg.error
                      ? { background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.3)', color: '#fca5a5' }
                      : { background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.08)', color: '#e2e8f0' }
                }
              >
                {isUser ? (
                  <p className="whitespace-pre-wrap break-words">{msg.content ?? msg.error}</p>
                ) : (
                  <div className="prose-overlay">
                    <ReactMarkdown
                      components={{
                        p: ({ children }) => <p className="whitespace-pre-wrap break-words">{children}</p>,
                        em: ({ children }) => <em style={{ color: '#94a3b8' }}>{children}</em>,
                      }}
                    >
                      {formatTagsForDisplay(cleanContent(msg.content)) ?? msg.error ?? ''}
                    </ReactMarkdown>
                  </div>
                )}
                {/* Long-message consent: capped auto-read offers the rest */}
                {!isUser &&
                  reader.hasPendingContinuation() &&
                  reader.partial?.fullRaw === msg.content && (
                  <button
                    onClick={() => reader.continueReading()}
                    className="mt-1.5 text-[10px] text-sky-400 hover:text-sky-300 border border-sky-500/30 rounded px-1.5 py-0.5 transition-colors no-drag"
                    title="Reading stopped at a natural break — continue (pre-rendered, resumes instantly)"
                  >
                    continue reading ▸
                  </button>
                )}
              </div>
              {!isUser && readBtn}
            </div>
          )
        })}

        {/* Loading indicator with status label */}
        {loading && (
          <div className="flex justify-start">
            <div
              className="rounded-xl px-3 py-2 flex items-center gap-2"
              style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.08)' }}
            >
              <span className="inline-flex gap-1">
                <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '-0.3s' }} />
                <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '-0.15s' }} />
                <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" />
              </span>
              {status && (
                <span className="text-xs text-slate-500 italic">{status}</span>
              )}
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* ── Cursor-aim countdown (camera button path) ─────────────────────── */}
      {aimCountdown > 0 && (
        <div className="flex-shrink-0 px-3 py-1 no-drag" style={{ background: 'rgba(5,8,14,0.9)' }}>
          <span className="text-[11px] font-semibold text-amber-300 animate-pulse">
            🎯 point your cursor at the target — capturing in {aimCountdown}…
          </span>
        </div>
      )}

      {/* ── Armed screenshot chip (O3): armed → attached / expired ──────────── */}
      {armedChip && (
        <div className="flex-shrink-0 px-3 py-1 no-drag" style={{ background: 'rgba(5,8,14,0.9)' }}>
          {armedChip.state === 'armed' && (
            <ArmedCountdown until={armedChip.until} />
          )}
          {armedChip.state === 'attached' && (
            <span className="text-[11px] font-semibold text-emerald-300">📸 attached — the agent is looking at it</span>
          )}
          {armedChip.state === 'expired' && (
            <span className="text-[11px] text-slate-500">📸 screenshot expired (speak within 15s next time)</span>
          )}
        </div>
      )}

      {/* ── Live captions (O1) — only while voice is connected ────────────────
          High-contrast styling so they stay readable over game footage even
          at low overlay opacity: near-solid backdrop + text shadow. */}
      {voiceState !== 'off' && (voiceState !== 'on' || userCaption || botCaption) && (
        <div
          className="flex-shrink-0 px-3 py-2 space-y-1 no-drag"
          style={{
            background: 'rgba(5, 8, 14, 0.92)',
            borderTop: '1px solid rgba(255,255,255,0.10)',
            textShadow: '0 1px 3px rgba(0,0,0,0.9)',
          }}
        >
          {voiceState === 'connecting' && (
            <p className="text-xs text-amber-300">Connecting voice…</p>
          )}
          {voiceState === 'error' && (
            <p className="text-xs text-red-300">Voice: {voiceError || 'connection failed'}</p>
          )}
          {userCaption && (
            <p className={`text-xs ${userCaption.final ? 'text-emerald-200' : 'text-emerald-200/60 italic'}`}>
              <span className="text-emerald-400/80 font-semibold">You: </span>
              {userCaption.text}
            </p>
          )}
          {botCaption && (
            <p className="text-sm text-slate-100 leading-snug">
              <span className={`font-semibold ${botSpeaking ? 'text-sky-300' : 'text-sky-400/70'}`}>the agent: </span>
              {botCaption}
            </p>
          )}
        </div>
      )}

      {/* ── Reader controls (R4) — MAIN-thread typed surface only ───────────── */}
      {textThread === 'main' && (
        <div
          className="flex-shrink-0 px-2 py-1 flex items-center gap-1 no-drag"
          style={{ background: 'rgba(5,8,14,0.7)', borderTop: '1px solid rgba(255,255,255,0.06)' }}
        >
          <button
            onClick={toggleAutoRead}
            className={`px-1.5 py-0.5 rounded text-[10px] font-semibold tracking-wide transition-colors ${
              readerAutoRead
                ? 'bg-sky-500/20 text-sky-300 border border-sky-500/40'
                : 'bg-white/5 text-slate-500 border border-white/10 hover:text-slate-300'
            }`}
            title={readerAutoRead ? 'Auto-read ON — replies are performed aloud' : 'Auto-read OFF — click to enable (also unlocks audio)'}
          >
            🔊 auto {readerAutoRead ? 'ON' : 'OFF'}
          </button>
          <button
            onClick={() => reader.stop()}
            disabled={!reader.playing}
            className="px-1.5 py-0.5 rounded text-[11px] text-slate-400 hover:text-slate-200 border border-white/10 disabled:opacity-30 transition-colors"
            title="Stop reading (cancels the render too)"
          >
            ⏹
          </button>
          <button
            onClick={() => reader.replayLast()}
            disabled={!reader.last || reader.playing}
            className="px-1.5 py-0.5 rounded text-[11px] text-slate-400 hover:text-slate-200 border border-white/10 disabled:opacity-30 transition-colors"
            title="Replay last reading — the exact same take"
          >
            ↻
          </button>
          <button
            onClick={() => reader.reroll()}
            disabled={!reader.last || reader.playing}
            className="px-1.5 py-0.5 rounded text-[11px] text-slate-400 hover:text-slate-200 border border-white/10 disabled:opacity-30 transition-colors"
            title="Re-roll — same text, fresh take"
          >
            🎲
          </button>
          {readerAutoRead && (voiceState === 'on' || voiceState === 'connecting') && deferToVoiceRef.current && (
            <span className="text-[10px] text-amber-400/80 ml-1" title="Voice session live — auto-read paused (readerDeferToVoice)">
              deferring to voice
            </span>
          )}
        </div>
      )}

      {/* ── Input Area (isolated to avoid re-rendering messages on every keystroke) ─ */}
      <InputArea
        onSend={sendMessage}
        loading={loading}
        clickThrough={clickThrough}
        pendingScreenshot={pendingScreenshot}
        onRemoveScreenshot={() => setPendingScreenshot(null)}
        onScreenshot={() => handleScreenshot(true)}
        screenshotLoading={screenshotLoading}
        error={error}
        isElectron={isElectron}
        inputRef={inputRef}
        textThread={textThread}
        onToggleThread={toggleThread}
        onPasteImages={(urls) =>
          setPendingScreenshot((prev) => {
            const cur = Array.isArray(prev) ? prev : prev ? [prev] : []
            return [...cur, ...urls]
          })
        }
      />
    </div>
  )
}
