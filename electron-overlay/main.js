const { app, BrowserWindow, ipcMain, globalShortcut, screen, desktopCapturer, nativeImage } = require('electron')
const path = require('path')
const fs = require('fs')

const isDev = process.env.NODE_ENV === 'development' || process.env.ELECTRON_DEV === '1'

let mainWindow

// ── API credential resolution (O0) ────────────────────────────────────────────
// the agent's API is Basic-auth gated (DASHBOARD_PASSWORD). Resolution order:
//   1. env (covers the agent-summoned launches via POST /api/overlay/launch,
//      which inherit the API process env)
//   2. electron-overlay/.env (gitignored local override)
//   3. ../.env — the agent's own .env (overlay lives inside the LANGGRAPH repo);
//      same no-duplication pattern the voice pipeline uses.
// The password is never hardcoded and never committed.
function readEnvValue(filePath, key) {
  try {
    for (const line of fs.readFileSync(filePath, 'utf-8').split(/\r?\n/)) {
      if (line.startsWith(`${key}=`)) return line.slice(key.length + 1).trim()
    }
  } catch { /* file missing/unreadable — fall through */ }
  return null
}

function resolveApiPassword() {
  return (
    process.env.AGENT_OVERLAY_PASSWORD ||
    process.env.DASHBOARD_PASSWORD ||
    readEnvValue(path.join(__dirname, '.env'), 'DASHBOARD_PASSWORD') ||
    readEnvValue(path.join(__dirname, '..', '.env'), 'DASHBOARD_PASSWORD') ||
    null
  )
}

function createWindow() {
  const { width: screenWidth, height: screenHeight } = screen.getPrimaryDisplay().workAreaSize

  mainWindow = new BrowserWindow({
    width: 380,
    height: 520,
    // Start in bottom-right corner, WoW-friendly position
    x: screenWidth - 400,
    y: screenHeight - 560,
    frame: false,           // No OS chrome — we draw our own title bar
    transparent: true,      // Enables the translucent background
    alwaysOnTop: true,      // Stays above WoW
    resizable: true,
    skipTaskbar: false,
    hasShadow: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  // Keep always-on-top even when WoW goes fullscreen (screen-saver level)
  mainWindow.setAlwaysOnTop(true, 'screen-saver')

  if (isDev) {
    mainWindow.loadURL('http://localhost:5174')
    // Uncomment to debug overlay fetch issues:
    // mainWindow.webContents.openDevTools({ mode: 'detach' })
  } else {
    mainWindow.loadFile(path.join(__dirname, 'dist', 'index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

// ── Hotkeys (O2): all bindings live in settings.json and avoid Shift entirely
// (Sticky Keys fires on 5 rapid Shift presses — the user's incident). Defaults:
const DEFAULT_HOTKEYS = {
  showHide: 'Ctrl+Alt+R',
  screenshot: 'Ctrl+Alt+S',
  clickThrough: 'Ctrl+Alt+C',
  voice: 'Ctrl+Alt+V',
  pttKey: 'F9',      // HOLD-to-talk via uiohook (globalShortcut has no key-release events)
  pttMouseButton: 5, // Razer paddle arrives as raw mouse button 5 when Synapse's
                     // software profile overrides the onboard F9 remap. 0 disables.
}

function getHotkeys() {
  return { ...DEFAULT_HOTKEYS, ...(readSettings().hotkeys || {}) }
}

// ── Push-to-talk (O2): uiohook in a DEDICATED utility process ────────────────
// Windows kills low-level hooks hosted in busy processes (LowLevelHooksTimeout)
// — running uiohook inside Electron main caused "worked → 5s lag → dead"
// (2026-07-03). ptt-watcher.js runs alone in a utilityProcess with an idle
// message pump; it debounces and posts {held}. Auto-respawns if it dies.
// Verified earlier: keyboard F9 and the user's Razer paddle both emit keycode 67.
const { utilityProcess } = require('electron')

let pttWatcher = null

function setupPtt() {
  const keyName = getHotkeys().pttKey

  const spawn = () => {
    pttWatcher = utilityProcess.fork(path.join(__dirname, 'ptt-watcher.js'), [], {
      serviceName: 'agent-ptt-watcher',
      stdio: 'inherit',
    })
    pttWatcher.postMessage({ pttKey: keyName, pttMouseButton: getHotkeys().pttMouseButton })
    pttWatcher.on('message', (msg) => {
      if (typeof msg?.held === 'boolean') {
        mainWindow?.webContents.send('ptt-state', msg.held)
      }
    })
    pttWatcher.on('exit', (code) => {
      console.warn(`ptt-watcher exited (${code}) — respawning in 1s`)
      // Fail-safe: treat watcher death as PTT release so the mic can't stick open.
      mainWindow?.webContents.send('ptt-state', false)
      setTimeout(spawn, 1000)
    })
  }

  spawn()
}

app.whenReady().then(() => {
  createWindow()
  const keys = getHotkeys()

  // Toggle overlay visibility. SAFETY: hiding while the mic is open must close
  // the mic — the renderer force-closes on this signal before we hide.
  globalShortcut.register(keys.showHide, () => {
    if (!mainWindow) return
    if (mainWindow.isVisible()) {
      mainWindow.webContents.send('force-mic-close')
      mainWindow.hide()
    } else {
      mainWindow.show()
      mainWindow.focus()
    }
  })

  // Screenshot from anywhere
  globalShortcut.register(keys.screenshot, () => {
    if (mainWindow) {
      mainWindow.webContents.send('trigger-screenshot')
      mainWindow.show()
      mainWindow.focus()
    }
  })

  // Toggle click-through (undo when stuck)
  globalShortcut.register(keys.clickThrough, () => {
    if (mainWindow) {
      mainWindow.webContents.send('toggle-click-through')
    }
  })

  // Connect/disconnect the agent voice (O1)
  globalShortcut.register(keys.voice, () => {
    if (mainWindow) {
      mainWindow.webContents.send('toggle-voice')
      mainWindow.show()
    }
  })

  setupPtt()
})

app.on('will-quit', () => {
  globalShortcut.unregisterAll()
  try { pttWatcher?.kill() } catch { /* not running */ }
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

// ── IPC Handlers ──────────────────────────────────────────────────────────────

// Window dragging (frameless window needs manual drag)
ipcMain.on('window-drag-start', () => {})

// Minimize
ipcMain.on('window-minimize', () => {
  mainWindow?.minimize()
})

// Close / hide (we hide rather than quit so hotkey can bring it back)
// SAFETY (O2): hiding must never leave an open mic behind.
ipcMain.on('window-hide', () => {
  mainWindow?.webContents.send('force-mic-close')
  mainWindow?.hide()
})

// Quit completely
ipcMain.on('window-quit', () => {
  app.quit()
})

// Opacity change. O2: while the mic is OPEN we enforce an opacity floor so the
// indicator can't be invisible — BrowserWindow.setOpacity dims the whole window
// including the indicator, so a 0.2-opacity overlay would hide a hot mic.
let userOpacity = 1.0
let micOpenForOpacity = false

function applyOpacity() {
  const floor = micOpenForOpacity ? Math.max(userOpacity, 0.85) : userOpacity
  mainWindow?.setOpacity(floor)
}

ipcMain.on('set-opacity', (_, value) => {
  userOpacity = value
  applyOpacity()
})

// Renderer reports ACTUAL mic state changes (from the real track, not intent).
ipcMain.on('mic-open-changed', (_, open) => {
  micOpenForOpacity = !!open
  applyOpacity()
})

// Screenshot (O3 dual-image scheme, configurable via settings.json "screenshot"):
//   1. Context: full screen downscaled to ~1440 wide, JPEG q85 — layout/situation.
//   2. Detail: full-native-resolution crop (~800×600) centered on the cursor —
//      the thing the user is actually pointing at, pixel-sharp, PNG.
// Kimi limits (verified 2026-07-03): ≤4096×2160 per image, base64 only — both
// images are comfortably inside. Returns { images: [...], tookMs }.
const DEFAULT_SCREENSHOT = { dualImage: true, contextWidth: 1440, jpegQuality: 85, cropW: 800, cropH: 600 }

ipcMain.handle('capture-screenshot', async () => {
  const cfg = { ...DEFAULT_SCREENSHOT, ...(readSettings().screenshot || {}) }
  const t0 = Date.now()
  try {
    // Briefly hide the overlay so it doesn't appear in the screenshot
    const wasVisible = mainWindow?.isVisible()
    mainWindow?.hide()

    // Small delay to let the game re-render without the overlay
    await new Promise((r) => setTimeout(r, 200))

    // Capture the display the cursor is on, at NATIVE pixel resolution.
    const cursor = screen.getCursorScreenPoint()
    const display = screen.getDisplayNearestPoint(cursor)
    const nativeW = Math.round(display.size.width * display.scaleFactor)
    const nativeH = Math.round(display.size.height * display.scaleFactor)

    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: { width: nativeW, height: nativeH },
    })
    const source = sources.find((s) => s.display_id === String(display.id)) || sources[0]
    const native = source?.thumbnail
    if (!native || native.isEmpty()) throw new Error('empty capture')

    const images = []

    // 1. Context image: downscaled full screen
    const ctxW = Math.min(cfg.contextWidth, nativeW)
    const contextImg = native.resize({ width: ctxW })
    images.push(`data:image/jpeg;base64,${contextImg.toJPEG(cfg.jpegQuality).toString('base64')}`)

    // 2. Detail crop at cursor, native resolution (dual-image mode only)
    if (cfg.dualImage) {
      const capSize = native.getSize() // actual delivered size (may differ from requested)
      const sx = capSize.width / display.size.width   // display points -> capture px
      const sy = capSize.height / display.size.height
      const cx = (cursor.x - display.bounds.x) * sx
      const cy = (cursor.y - display.bounds.y) * sy
      const w = Math.min(cfg.cropW, capSize.width)
      const h = Math.min(cfg.cropH, capSize.height)
      const x = Math.round(Math.min(Math.max(cx - w / 2, 0), capSize.width - w))
      const y = Math.round(Math.min(Math.max(cy - h / 2, 0), capSize.height - h))
      const crop = native.crop({ x, y, width: w, height: h })
      images.push(`data:image/png;base64,${crop.toPNG().toString('base64')}`)
    }

    if (wasVisible) mainWindow?.show()
    return { images, tookMs: Date.now() - t0 }
  } catch (err) {
    console.error('capture failed:', err)
    mainWindow?.show()
    return null
  }
})

// Click-through toggle: when enabled, mouse clicks pass through to WoW
ipcMain.on('set-click-through', (_, enabled) => {
  mainWindow?.setIgnoreMouseEvents(enabled, { forward: true })
})

// API auth: renderer asks once at startup; returns the ready-made header value
// (or null if no credential found — renderer shows a helpful error).
ipcMain.handle('get-api-auth', () => {
  const pw = resolveApiPassword()
  if (!pw) return null
  return `Basic ${Buffer.from(`overlay:${pw}`).toString('base64')}`
})

// ── Overlay settings (gitignored settings.json) ──────────────────────────────
// Holds user prefs: text thread choice now; configurable hotkeys at O2.
const SETTINGS_PATH = path.join(__dirname, 'settings.json')

function readSettings() {
  try {
    return JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf-8'))
  } catch {
    return {}
  }
}

ipcMain.handle('get-settings', () => readSettings())

ipcMain.handle('set-settings', (_, patch) => {
  const merged = { ...readSettings(), ...(patch || {}) }
  try {
    fs.writeFileSync(SETTINGS_PATH, JSON.stringify(merged, null, 2))
  } catch (err) {
    console.error('settings write failed:', err)
  }
  return merged
})
