r"""M4: talk to the REAL the agent. Echo-loop pipeline with the parrot replaced by a
service that streams from the agent's POST /api/chat/stream (SSE) and sentence-chunks
tokens into TTS as they arrive.

Reuses the signed-off M3 services by importing them from echo_loop (which also
preloads Chatterbox + whisper and logs VRAM).

Config (env / voice/.env):
  AGENT_API_BASE      default http://127.0.0.1:8000
  AGENT_API_PASSWORD  Basic auth password. If unset, read DASHBOARD_PASSWORD
                      directly from the repo root .env (same machine, no
                      secret duplication).
  AGENT_USE_KNOWLEDGE "true" to enable knowledge RAG for the session (default off)

Run:   $env:PYTHONUTF8='1'; .venv\Scripts\python voice_bot.py -t webrtc --port 8010
"""
import torch  # noqa: F401  (torch first — cuDNN DLLs for ctranslate2; see DECISIONS.md)

import asyncio
import base64
import json
import os
import random
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

# Reuse M3's signed-off services + preloaded models (import runs the preload).
from echo_loop import (
    CHATTERBOX,
    REF_VOICE,
    BargeIn,
    ChatterboxTurboTTSService,
    SharedWhisperSTTService,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

AGENT_API_BASE = os.environ.get("AGENT_API_BASE", "http://127.0.0.1:8000").rstrip("/")
AGENT_USE_KNOWLEDGE = os.environ.get("AGENT_USE_KNOWLEDGE", "").strip().lower() == "true"

# Half-duplex ("push to talk"-ish) mode: while the agent is speaking, mic input can
# neither interrupt him nor queue a new turn — background noise can't cut him
# off. Hands-free listening resumes automatically the moment he goes quiet.
# Trade-off (the user-accepted): deliberate barge-in is also disabled while on.
VOICE_HALF_DUPLEX = os.environ.get("VOICE_HALF_DUPLEX", "").strip().lower() == "true"


class BotSpeakingState:
    """Shared bot-is-speaking flag, updated from Bot*SpeakingFrames flowing
    upstream from the output transport."""

    def __init__(self):
        self.speaking = False


BOT_SPEAKING = BotSpeakingState()

_SENTENCE_END = re.compile(r"([.!?…][\"'\)\]]?)(\s|$)")
_MIN_SENTENCE_CHARS = 12  # don't TTS fragments like "Dr." or "1."

# ---- Filler acknowledgment clips (M5, retuned post-Option-C) ----------------
# Pre-generated in the agent's voice by make_fillers.py (3 sampling takes per phrase,
# filenames <phrase>_t<n>.wav). Rules (the user):
# - first filler only when the wait exceeds FILLER_FIRST_S (2.5s: short waits
#   stay silent) AND only for ~FILLER_PROB of eligible pauses (rest stay quiet)
# - second filler ONLY on tool rounds ("checking_notes" group, deterministic)
# - max two per turn; never the same take (filename) twice in a row
# - cancelled when real speech arrives; interruptible like any bot audio
FILLER_FIRST_S = float(os.environ.get("FILLER_FIRST_S", "2.5"))
FILLER_PROB = float(os.environ.get("FILLER_PROB", "0.6"))
_FILLER_DIR = Path(__file__).resolve().parent / "fillers"
_TOOL_PHRASE = "checking_notes"


def _load_fillers() -> dict[str, tuple[bytes, int]]:
    """Load filler wavs as 16-bit PCM. torchaudio saved them as float32 WAVs,
    which stdlib `wave` can't read — soundfile handles both and converts."""
    import numpy as np
    import soundfile as sf

    out: dict[str, tuple[bytes, int]] = {}
    for path in sorted(_FILLER_DIR.glob("*.wav")):
        data, rate = sf.read(str(path), dtype="float32", always_2d=False)
        pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        out[path.stem] = (pcm, rate)
    logger.info(f"loaded {len(out)} filler takes: {', '.join(out)}")
    return out


def _phrase_of(take_name: str) -> str:
    return take_name.rsplit("_t", 1)[0]


FILLERS = _load_fillers()
GENERAL_TAKES = [n for n in FILLERS if _phrase_of(n) != _TOOL_PHRASE]
TOOL_TAKES = [n for n in FILLERS if _phrase_of(n) == _TOOL_PHRASE]


def _get_agent_password() -> str:
    pw = os.environ.get("AGENT_API_PASSWORD", "").strip()
    if pw:
        return pw
    agent_env = Path(__file__).resolve().parents[1] / ".env"
    if agent_env.exists():
        for line in agent_env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DASHBOARD_PASSWORD="):
                return line.split("=", 1)[1].strip()
    return ""


def _auth_header() -> dict:
    pw = _get_agent_password()
    if not pw:
        logger.warning("No the agent API password found — requests will fail if auth is on")
        return {}
    token = base64.b64encode(f"voice:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class PttGate(FrameProcessor):
    """Server-side push-to-talk gate + explicit barge-in (O2, the user's design).

    Why server-side: gating the client track (track.enabled=false) stops RTP
    entirely, and the SmallWebRTC connection's idle watcher permanently
    disables the audio receiver after 2s of silence — audio never recovers
    when PTT opens (root cause of the 2026-07-03 "words never reach the agent"
    bug). So the client keeps its track live and instead sends data-channel
    messages; this gate drops InputAudioRawFrames while closed.

    Per-connection self-selection: the gate is OPEN until the first ptt
    message arrives (open-mic clients like the Playground never send one and
    are unaffected). The overlay announces PTT mode by sending 'ptt-close'
    immediately on connect. Fail-safe direction: once a client has announced
    PTT, a desync means audio is DROPPED, never leaked.

    'ptt-interrupt' (sent on press while the agent speaks) broadcasts the
    interruption — the press itself is the barge-in signal, no VAD needed.
    """

    def __init__(self):
        super().__init__()
        self._ptt_client = False   # becomes True on the first ptt message
        self._gate_open = True     # irrelevant until _ptt_client

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputTransportMessageFrame):
            msg = str(getattr(frame, "message", ""))
            if "ptt-interrupt" in msg:
                logger.info("ptt: paddle press while bot speaking — broadcasting interruption")
                await self.broadcast_interruption()
                return  # consumed
            if "ptt-open" in msg:
                if not self._ptt_client:
                    logger.info("ptt: client announced PTT mode")
                self._ptt_client = True
                self._gate_open = True
                logger.debug("ptt: gate OPEN")
                return  # consumed
            if "ptt-close" in msg:
                if not self._ptt_client:
                    logger.info("ptt: client announced PTT mode")
                self._ptt_client = True
                self._gate_open = False
                logger.debug("ptt: gate CLOSED")
                return  # consumed

        if self._ptt_client and not self._gate_open and isinstance(frame, InputAudioRawFrame):
            return  # gated: drop mic audio while PTT not engaged

        await self.push_frame(frame, direction)


class VoiceBargeIn(BargeIn):
    """BargeIn that honors half-duplex mode: while the bot is speaking, user
    speech does NOT broadcast an interruption. Tracks Bot*SpeakingFrames
    (pushed upstream by the output transport) in the shared state."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, BotStartedSpeakingFrame):
            BOT_SPEAKING.speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            BOT_SPEAKING.speaking = False

        if (
            VOICE_HALF_DUPLEX
            and BOT_SPEAKING.speaking
            and isinstance(frame, VADUserStartedSpeakingFrame)
        ):
            # Swallow the barge-in trigger; pass the VAD frame through so STT
            # still segments (the transcription gets dropped downstream too).
            logger.debug("half-duplex: user speech during bot speech — no interruption")
            await FrameProcessor.process_frame(self, frame, direction)
            await self.push_frame(frame, direction)
            return

        await super().process_frame(frame, direction)


class AgentLLMService(FrameProcessor):
    """Streams the agent's reply for each final transcription; speaks sentence by sentence.

    On barge-in (InterruptionFrame) the in-flight SSE request is cancelled;
    the agent's server finishes the turn internally so history stays consistent.
    """

    def __init__(self):
        super().__init__()
        self._task: asyncio.Task | None = None
        self._filler_task: asyncio.Task | None = None
        self._last_filler: str | None = None  # persists across turns (no repeats)
        self._fillers_this_turn = 0
        self._spoke_this_turn = False

    def _cancel_tasks(self) -> None:
        for t in (self._task, self._filler_task):
            if t and not t.done():
                t.cancel()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            BOT_SPEAKING.speaking = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            BOT_SPEAKING.speaking = False
            await self.push_frame(frame, direction)
        elif isinstance(frame, InterruptionFrame):
            if (self._task and not self._task.done()) or (self._filler_task and not self._filler_task.done()):
                logger.info("agent: barge-in — cancelling in-flight stream + fillers")
            self._cancel_tasks()
            await self.push_frame(frame, direction)
        elif isinstance(frame, TranscriptionFrame):
            # Half-duplex: speech transcribed while the bot was talking is noise
            # by definition — drop it so it can't queue a turn. (A segment that
            # *ends* just after he stops still gets through; acceptable edge.)
            if VOICE_HALF_DUPLEX and BOT_SPEAKING.speaking:
                logger.info(f"half-duplex: dropping transcript during bot speech: {frame.text!r}")
                return
            logger.info(f"agent <- {frame.text!r}")
            self._cancel_tasks()
            self._fillers_this_turn = 0
            self._spoke_this_turn = False
            self._task = asyncio.create_task(self._stream_reply(frame.text))
            self._filler_task = asyncio.create_task(self._filler_watchdog())
        else:
            await self.push_frame(frame, direction)

    # ---- fillers ----
    async def _play_filler(self, take_name: str) -> None:
        if self._fillers_this_turn >= 2 or self._spoke_this_turn:
            return
        pcm, rate = FILLERS[take_name]
        self._fillers_this_turn += 1
        self._last_filler = take_name
        logger.info(f"agent: filler #{self._fillers_this_turn} -> {take_name}")
        chunk = int(rate * 0.1) * 2  # 100ms of 16-bit mono — interruptible granularity
        for i in range(0, len(pcm), chunk):
            await self.push_frame(TTSAudioRawFrame(pcm[i:i + chunk], rate, 1))

    def _pick_take(self, pool: list[str]) -> str:
        """Random take, never the same file twice in a row (across turns)."""
        options = [n for n in pool if n != self._last_filler] or pool
        return random.choice(options)

    async def _filler_watchdog(self) -> None:
        """One general filler if the wait exceeds FILLER_FIRST_S — and only for
        ~FILLER_PROB of eligible pauses; the rest stay quiet. The second filler
        slot is reserved for tool rounds (checking_notes, in _stream_reply)."""
        try:
            await asyncio.sleep(FILLER_FIRST_S)
            if random.random() < FILLER_PROB:
                await self._play_filler(self._pick_take(GENERAL_TAKES))
            else:
                logger.debug("agent: eligible pause, staying quiet (probabilistic)")
        except asyncio.CancelledError:
            pass

    async def _say(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            # Real speech arriving: no further fillers this turn.
            self._spoke_this_turn = True
            if self._filler_task and not self._filler_task.done():
                self._filler_task.cancel()
            logger.info(f"agent -> speak: {sentence!r}")
            await self.push_frame(TTSSpeakFrame(sentence))

    async def _stream_reply(self, user_text: str) -> None:
        t0 = time.perf_counter()
        first_token_s: float | None = None
        first_sentence_s: float | None = None
        buffer = ""
        body = {
            "message": user_text,
            "thread_id": "voice",
            # "local" = auto-recall ON (fast since the TEI reranker: ~1-2s).
            # Was "internal" while recall cost 5-11s. See DECISIONS.md.
            "channel_type": "local",
            "voice_mode": True,
            "use_knowledge": AGENT_USE_KNOWLEDGE,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10)) as client:
                async with client.stream(
                    "POST", f"{AGENT_API_BASE}/api/chat/stream",
                    json=body, headers=_auth_header(),
                ) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        logger.error(f"agent: HTTP {resp.status_code} from /api/chat/stream")
                        await self._say("Sorry, I couldn't reach my brain just now.")
                        return
                    event = None
                    async for line in resp.aiter_lines():
                        if line.startswith("event: "):
                            event = line[7:]
                            continue
                        if not line.startswith("data: "):
                            continue
                        data = json.loads(line[6:])
                        if event == "token":
                            if first_token_s is None:
                                first_token_s = time.perf_counter() - t0
                                logger.info(f"agent: first token after {first_token_s:.2f}s")
                            buffer += data["t"]
                            # Emit complete sentences as they form.
                            while True:
                                m = _SENTENCE_END.search(buffer)
                                if not m or m.end(1) < _MIN_SENTENCE_CHARS:
                                    break
                                sentence, buffer = buffer[:m.end(1)], buffer[m.end(1):]
                                if first_sentence_s is None:
                                    first_sentence_s = time.perf_counter() - t0
                                    logger.info(f"agent: first sentence after {first_sentence_s:.2f}s")
                                await self._say(sentence)
                        elif event in ("boundary", "done"):
                            await self._say(buffer)
                            buffer = ""
                            if event == "boundary" and not self._spoke_this_turn:
                                # Silent LLM round just ended -> a tool is about to
                                # run. Speak the tool-specific acknowledgment.
                                await self._play_filler(self._pick_take(TOOL_TAKES))
                            if event == "done":
                                total = time.perf_counter() - t0
                                logger.info(
                                    f"agent: turn done in {total:.2f}s "
                                    f"(first token {first_token_s and f'{first_token_s:.2f}'}s, "
                                    f"first sentence {first_sentence_s and f'{first_sentence_s:.2f}'}s, "
                                    f"server timings {data.get('timings')})"
                                )
                        elif event == "error":
                            logger.error(f"agent: stream error: {data.get('message')}")
                            await self._say("Sorry, something went wrong on my end.")
        except asyncio.CancelledError:
            logger.info("agent: stream cancelled (barge-in or new turn)")
            raise
        except Exception:
            logger.exception("agent: stream failed")
            await self._say("Sorry, I hit an error talking to my brain.")


async def bot(runner_args: RunnerArguments) -> None:
    assert isinstance(runner_args, SmallWebRTCRunnerArguments), "webrtc transport only"

    transport = SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_10ms_chunks=2,
        ),
    )
    # Same tuned VAD as M3 sign-off (see DECISIONS.md).
    vad_params = VADParams(confidence=0.6, start_secs=0.15, stop_secs=0.8, min_volume=0.35)
    pipeline = Pipeline([
        transport.input(),
        PttGate(),
        VADProcessor(vad_analyzer=SileroVADAnalyzer(params=vad_params)),
        VoiceBargeIn(),
        SharedWhisperSTTService(model="large-v3-turbo", device="cuda", compute_type="float16"),
        AgentLLMService(),
        ChatterboxTurboTTSService(CHATTERBOX, REF_VOICE),
        transport.output(),
    ])
    task = PipelineTask(pipeline)

    @transport.event_handler("on_client_connected")
    async def on_connected(t, c):
        logger.info(
            f"client connected — the agent voice live (knowledge={'on' if AGENT_USE_KNOWLEDGE else 'off'}, "
            f"half-duplex={'on' if VOICE_HALF_DUPLEX else 'off'})"
        )

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(t, c):
        logger.info("client disconnected — stopping pipeline")
        await task.cancel()

    await PipelineRunner().run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
