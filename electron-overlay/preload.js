const { contextBridge, ipcRenderer } = require('electron')

// Expose a safe, typed API to the renderer (React app)
contextBridge.exposeInMainWorld('electronAPI', {
  // Window controls
  minimize: () => ipcRenderer.send('window-minimize'),
  hide: () => ipcRenderer.send('window-hide'),
  quit: () => ipcRenderer.send('window-quit'),

  // Opacity (0.0 – 1.0)
  setOpacity: (value) => ipcRenderer.send('set-opacity', value),

  // Screenshot: resolves to a base64 data URL string or null
  captureScreenshot: () => ipcRenderer.invoke('capture-screenshot'),

  // API auth: resolves to an Authorization header value string or null
  getApiAuth: () => ipcRenderer.invoke('get-api-auth'),

  // Overlay settings (settings.json): read all / merge-write a patch
  getSettings: () => ipcRenderer.invoke('get-settings'),
  setSettings: (patch) => ipcRenderer.invoke('set-settings', patch),

  // Click-through mode (mouse passes to WoW)
  setClickThrough: (enabled) => ipcRenderer.send('set-click-through', enabled),

  // Listen for hotkey-triggered screenshot from main process
  onTriggerScreenshot: (callback) => {
    ipcRenderer.on('trigger-screenshot', callback)
    return () => ipcRenderer.removeListener('trigger-screenshot', callback)
  },

  // Listen for hotkey to toggle click-through (undo when stuck)
  onToggleClickThrough: (callback) => {
    ipcRenderer.on('toggle-click-through', callback)
    return () => ipcRenderer.removeListener('toggle-click-through', callback)
  },

  // Listen for voice connect/disconnect hotkey (O1; Ctrl+Alt+V as of O2)
  onToggleVoice: (callback) => {
    ipcRenderer.on('toggle-voice', callback)
    return () => ipcRenderer.removeListener('toggle-voice', callback)
  },

  // O2 push-to-talk: main process sends debounced hold state (true=held)
  onPttState: (callback) => {
    const listener = (_, held) => callback(held)
    ipcRenderer.on('ptt-state', listener)
    return () => ipcRenderer.removeListener('ptt-state', listener)
  },

  // O2 safety: main demands the mic close NOW (hide, etc.)
  onForceMicClose: (callback) => {
    ipcRenderer.on('force-mic-close', callback)
    return () => ipcRenderer.removeListener('force-mic-close', callback)
  },

  // O2: renderer reports ACTUAL mic-open state (drives the opacity floor)
  reportMicOpen: (open) => ipcRenderer.send('mic-open-changed', open),
})
