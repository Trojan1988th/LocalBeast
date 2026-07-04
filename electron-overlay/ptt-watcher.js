// PTT watcher — runs as an Electron utilityProcess (its own Node process).
//
// Why not in main: Windows silently REMOVES low-level keyboard hooks when the
// hosting process's message pump is slow (LowLevelHooksTimeout). Electron's
// main process gets busy enough to trigger this — observed 2026-07-03 as
// "F9 worked, then lagged 5s, then died entirely". A dedicated process has
// nothing else to do, so its hook callback always returns instantly.
//
// TWO input paths, one state machine (2026-07-03, the user's paddle):
//   - keyboard key (default F9)
//   - mouse button (default 5 — the Razer paddle arrives as raw button 5 when
//     Synapse's software profile overrides the onboard F9 remap; covering both
//     means PTT works regardless of which profile Synapse applies)
//
// Protocol: parentPort receives {pttKey, pttMouseButton}; posts {held} after
// the debounce. Debounce: opens only if still held after 100ms (a graze never
// opens); auto-repeats ignored; only the source that opened can close.
const { uIOhook, UiohookKey } = require('uiohook-napi')

const PTT_DEBOUNCE_MS = 100
let pttKeycode = UiohookKey.F9
let pttMouseButton = 5 // 0 disables the mouse path
let activeSource = null // 'key' | 'mouse' | null
let debounceTimer = null
let debounceSource = null

process.parentPort.on('message', (e) => {
  const keyName = e.data?.pttKey
  if (keyName && UiohookKey[keyName]) {
    pttKeycode = UiohookKey[keyName]
    console.log(`[ptt-watcher] key set to ${keyName} (keycode ${pttKeycode})`)
  }
  if (typeof e.data?.pttMouseButton === 'number') {
    pttMouseButton = e.data.pttMouseButton
    console.log(`[ptt-watcher] mouse button set to ${pttMouseButton}`)
  }
})

function down(source) {
  if (activeSource || debounceTimer) return // already held/arming
  debounceSource = source
  debounceTimer = setTimeout(() => {
    debounceTimer = null
    activeSource = source
    process.parentPort.postMessage({ held: true })
  }, PTT_DEBOUNCE_MS)
}

function up(source) {
  if (debounceTimer && debounceSource === source) {
    clearTimeout(debounceTimer) // released within debounce — a graze
    debounceTimer = null
    return
  }
  if (activeSource === source) {
    activeSource = null
    process.parentPort.postMessage({ held: false })
  }
}

uIOhook.on('keydown', (e) => {
  if (e.keycode === pttKeycode) down('key')
})
uIOhook.on('keyup', (e) => {
  if (e.keycode === pttKeycode) up('key')
})
uIOhook.on('mousedown', (e) => {
  if (pttMouseButton && e.button === pttMouseButton) down('mouse')
})
uIOhook.on('mouseup', (e) => {
  if (pttMouseButton && e.button === pttMouseButton) up('mouse')
})

uIOhook.start()
console.log('[ptt-watcher] hook started (keyboard + mouse)')
