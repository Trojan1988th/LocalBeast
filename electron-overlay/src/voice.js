// Voice client for the overlay (O1) — pipecat client-js + SmallWebRTC transport
// talking to the existing voice_bot on :8010 (the same Phase A pipeline the
// voice page uses: VAD, whisper, Chatterbox in the agent's voice, fillers, barge-in).
//
// Design: one long-lived wrapper object owned by React state. Disconnected by
// default at launch; connect/disconnect via UI button or Ctrl+Shift+V.
import { PipecatClient, RTVIEvent } from '@pipecat-ai/client-js'
import { SmallWebRTCTransport } from '@pipecat-ai/small-webrtc-transport'

const VOICE_BOT_URL = 'http://localhost:8010'

export class VoiceClient {
  /**
   * @param {object} handlers
   *   onState(state: 'off'|'connecting'|'on'|'error', detail?: string)
   *   onUserCaption(text: string, final: boolean)
   *   onBotCaption(text: string)   // accumulated text for the current bot turn
   *   onBotSpeaking(speaking: boolean)
   */
  constructor(handlers) {
    this.h = handlers
    this.client = null
    this.audioEl = null
    this._botCaption = ''
    this._botTtsSeen = false
    this._micCommanded = false
    // O2: explicit input device (OBS coexistence — the user streams regularly).
    // null = current behavior (system default).
    this.micDeviceId = null
  }

  // Device picker support (O2). Labels are only available after a mic
  // permission grant, i.e. after the first voice session.
  async getAllMics() {
    try {
      if (this.client) return await this.client.getAllMics()
      const devices = await navigator.mediaDevices.enumerateDevices()
      return devices.filter((d) => d.kind === 'audioinput')
    } catch {
      return []
    }
  }

  updateMic(deviceId) {
    this.micDeviceId = deviceId || null
    try {
      if (this.client && this.micDeviceId) this.client.updateMic(this.micDeviceId)
    } catch { /* applied at next connect */ }
  }

  get connected() {
    return !!this.client
  }

  // O2 PTT: the gate lives SERVER-SIDE (voice_bot's PttGate) and is driven by
  // data-channel messages. The local track always stays live and enabled:
  //  - client.enableMic(false): Daily manager ends/reacquires tracks in a loop
  //  - raw track.enabled=false: browser stops sending RTP entirely, and the
  //    server's idle watcher permanently disables the receiver after 2s —
  //    audio never recovers when PTT opens (the "words never reach the agent" bug)
  // Fail-safe: once we've announced PTT mode (ptt-close at connect), a lost
  // message means the server keeps DROPPING audio — never leaking it.
  setMicEnabled(enabled) {
    try {
      this._micCommanded = !!enabled
      this.client?.sendClientMessage(enabled ? 'ptt-open' : 'ptt-close', {})
    } catch { /* not connected */ }
  }

  // O2 barge-in (the user's design): the PTT PRESS is the interruption signal —
  // explicit and client-initiated, not dependent on VAD hearing speech.
  // voice_bot's PttInterrupt processor turns this into the pipeline broadcast.
  sendPttInterrupt() {
    try {
      this.client?.sendClientMessage('ptt-interrupt', {})
    } catch { /* not connected */ }
  }

  // O2 indicator: open = a live local track exists AND the gate was commanded
  // open. The dangerous desync (indicator dark while audio processes) can't
  // happen: the server gate defaults CLOSED for PTT clients, so a lost command
  // fails toward silence. If the track itself dies, this reads false.
  isMicActuallyOpen() {
    try {
      const track = this.client?.tracks()?.local?.audio
      return !!(track && track.readyState === 'live' && this._micCommanded)
    } catch {
      return false
    }
  }

  async connect() {
    if (this.client) return
    this.h.onState('connecting')
    try {
      const client = new PipecatClient({
        transport: new SmallWebRTCTransport({
          iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
        }),
        enableCam: false,
        // O2 push-to-talk: constructing with enableMic:false hangs this
        // transport version's mediaManager.connect() before the offer is ever
        // sent (verified against 1.10.5). So: acquire the mic normally, then
        // close it in onConnected below — the track is disabled within the
        // same tick the connection lands. From then on only F9 opens it.
        enableMic: true,
        callbacks: {
          onConnected: () => {
            // Announce PTT mode: this first ptt-close engages the server-side
            // gate for this connection (open-mic clients never send one).
            this.setMicEnabled(false)
            this.h.onState('on')
          },
          onDisconnected: () => {
            this._teardown()
            this.h.onState('off')
          },
          onError: (msg) => {
            // Covers the two-client case too: if voice_bot refuses or the
            // session dies, we surface it and return to a clean 'off' state.
            const detail = msg?.data?.message || msg?.message || 'voice connection error'
            this.h.onState('error', String(detail))
            this.disconnect()
          },
          onUserTranscript: (data) => {
            if (data?.text) this.h.onUserCaption(data.text, !!data.final)
            if (data?.final) this._resetBotCaption()
          },
          // Word-by-word text of what is actually being spoken (best sync).
          onBotTtsText: (data) => {
            if (!data?.text) return
            this._botTtsSeen = true
            this._botCaption = this._botCaption
              ? `${this._botCaption} ${data.text}`
              : data.text
            this.h.onBotCaption(this._botCaption)
          },
          // Fallback: aggregated bot output (used only if no TTS text events
          // arrive on this deployment — avoids double-rendering).
          onBotOutput: (data) => {
            if (this._botTtsSeen || !data?.text) return
            this._botCaption = this._botCaption
              ? `${this._botCaption} ${data.text}`
              : data.text
            this.h.onBotCaption(this._botCaption)
          },
          onBotStartedSpeaking: () => this.h.onBotSpeaking(true),
          onBotStoppedSpeaking: () => this.h.onBotSpeaking(false),
        },
      })

      // Bot audio: attach the remote track to a hidden <audio> element.
      client.on(RTVIEvent.TrackStarted, (track, participant) => {
        if (participant?.local || track.kind !== 'audio') return
        if (!this.audioEl) {
          this.audioEl = document.createElement('audio')
          this.audioEl.autoplay = true
          document.body.appendChild(this.audioEl)
        }
        this.audioEl.srcObject = new MediaStream([track])
      })

      this.client = client
      // Connect straight to the runner's offer route. (startBotAndConnect's
      // /start flow needs a response transformer the prebuilt UI has and we
      // don't — the raw client stalls silently after /start. The runner spawns
      // the bot from /api/offer as well, so direct connect is the documented
      // simple path.) 20s deadline so a half-dead handshake can't hang the UI.
      await Promise.race([
        // Runner protocol, proven at O1: POST /start (requestData must be a
        // JSON object or the runner can't parse the body), then the offer.
        client.startBotAndConnect({
          endpoint: `${VOICE_BOT_URL}/start`,
          requestData: {},
        }),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('voice connect timed out (20s)')), 20000)
        ),
      ])
      // Re-announce PTT mode now the bot handshake is complete — the
      // onConnected send can race the data channel/bot readiness.
      this.setMicEnabled(false)
      // Apply the explicitly chosen input device (OBS coexistence).
      if (this.micDeviceId) {
        try {
          this.client?.updateMic(this.micDeviceId)
        } catch { /* device may be unplugged — default persists */ }
      }
    } catch (err) {
      this._teardown()
      this.h.onState('error', err?.message || 'could not reach voice_bot on :8010')
    }
  }

  async disconnect() {
    const client = this.client
    // SAFETY (O2): force-close the mic before teardown — disconnecting must
    // never race a held PTT into a lingering open track.
    this.setMicEnabled(false)
    this._teardown()
    if (client) {
      try {
        await client.disconnect()
      } catch { /* already gone */ }
    }
    this.h.onState('off')
  }

  _resetBotCaption() {
    this._botCaption = ''
    this._botTtsSeen = false
  }

  _teardown() {
    this.client = null
    this._micCommanded = false
    this._resetBotCaption()
    if (this.audioEl) {
      this.audioEl.srcObject = null
      this.audioEl.remove()
      this.audioEl = null
    }
  }
}
