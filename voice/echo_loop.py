r"""M3: browser echo loop — proves WebRTC, VAD, STT, TTS, and barge-in end to end
with zero agent risk. The "LLM" is a parrot that repeats the transcript back.

Pipeline: SmallWebRTCTransport -> Silero VAD -> faster-whisper large-v3-turbo
          -> parrot -> Chatterbox-Turbo (your configured voice) -> browser audio.

Uses pipecat's development runner, which serves the /start + /api/offer protocol
the prebuilt Pipecat Playground UI (2.5.0) expects.

Run:   .venv\Scripts\python echo_loop.py -t webrtc --port 8010
Open:  http://localhost:8010  (optionally expose on your tailnet via
       `tailscale serve` for phone access)
"""
# IMPORTANT: torch first — its bundled cuDNN 9 / cuBLAS DLLs must be loaded
# before ctranslate2 (faster-whisper) initializes CUDA. See DECISIONS.md.
import torch

import asyncio
import os
import subprocess
from pathlib import Path

import numpy as np
from loguru import logger

from chatterbox.tts_turbo import ChatterboxTurboTTS
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.services.tts_service import TTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

ROOT = Path(__file__).resolve().parent
REF_VOICE = os.environ.get("VOICE_REF_WAV", "")  # your own ~10-30s reference recording (required)


def gpu_used_mb() -> int:
    """Total board VRAM in use (WDDM hides per-process numbers)."""
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )
    return int(out.stdout.strip().splitlines()[0])


class SharedWhisperSTTService(WhisperSTTService):
    """WhisperSTTService that shares one loaded model across connections
    (avoids a ~16s model load every time the browser reconnects)."""

    _shared_model = None

    def _load(self):
        if SharedWhisperSTTService._shared_model is None:
            super()._load()
            SharedWhisperSTTService._shared_model = self._model
        else:
            self._model = SharedWhisperSTTService._shared_model


class ChatterboxTurboTTSService(TTSService):
    """Chatterbox-Turbo TTS in your cloned voice (VOICE_REF_WAV reference)."""

    def __init__(self, chatterbox: ChatterboxTurboTTS, ref_voice: str, **kwargs):
        super().__init__(sample_rate=chatterbox.sr, **kwargs)
        self._chatterbox = chatterbox
        self._ref_voice = ref_voice

    def can_generate_metrics(self) -> bool:
        return True

    def _generate_pcm16(self, text: str) -> bytes:
        with torch.inference_mode():
            wav = self._chatterbox.generate(text, audio_prompt_path=self._ref_voice)
        pcm = wav.squeeze(0).clamp(-1.0, 1.0).cpu().numpy()
        return (pcm * 32767).astype(np.int16).tobytes()

    async def run_tts(self, text: str, context_id: str):
        logger.debug(f"ChatterboxTurbo TTS [{text}]")
        await self.start_tts_usage_metrics(text)

        # NOTE: chatterbox generation is a blocking CUDA call — it cannot be
        # cancelled mid-utterance. On barge-in the generator is closed at the
        # next yield, so a finished-but-unwanted utterance is discarded unplayed
        # (≤ ~2.5s of wasted GPU time, no audible effect).
        async def audio_iter():
            pcm = await asyncio.to_thread(self._generate_pcm16, text)
            # Small chunks give the pipeline frequent cancellation points, so
            # barge-in can drop remaining audio instead of one monolithic frame.
            CHUNK = 4800  # 100ms @ 24kHz mono int16
            for i in range(0, len(pcm), CHUNK):
                yield pcm[i:i + CHUNK]

        async for frame in self._stream_audio_frames_from_iterator(
            audio_iter(), in_sample_rate=self._chatterbox.sr, context_id=context_id
        ):
            await self.stop_ttfb_metrics()
            yield frame


class BargeIn(FrameProcessor):
    """Broadcasts an InterruptionFrame when the user starts speaking.

    In pipecat 1.4 this broadcast normally comes from the LLM user-context
    aggregator on user-turn start — this LLM-less echo pipeline has no
    aggregator, so without this processor nothing ever interrupts the bot
    (root cause of the M3 barge-in bug; see DECISIONS.md).
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            logger.info("barge-in: VAD user-speech start -> broadcasting InterruptionFrame")
            await self.broadcast_interruption()
        await self.push_frame(frame, direction)


class Parrot(FrameProcessor):
    """Echo 'LLM': turns each final transcription into a spoken reply."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            logger.info("parrot: InterruptionFrame received — bot speech cancelled")
            await self.push_frame(frame, direction)
        elif isinstance(frame, TranscriptionFrame):
            logger.info(f"parrot heard: {frame.text!r}")
            await self.push_frame(TTSSpeakFrame(f"You said: {frame.text}"))
        else:
            await self.push_frame(frame, direction)


# ---- Model preload (once per process) with VRAM accounting -----------------
vram0 = gpu_used_mb()
logger.info(f"VRAM baseline: {vram0} MiB")

CHATTERBOX = ChatterboxTurboTTS.from_pretrained(device="cuda")
vram1 = gpu_used_mb()
logger.info(f"VRAM after Chatterbox-Turbo: {vram1} MiB (+{vram1 - vram0})")

_warm = SharedWhisperSTTService(model="large-v3-turbo", device="cuda", compute_type="float16")
_warm._load()
vram2 = gpu_used_mb()
logger.info(f"VRAM after whisper large-v3-turbo: {vram2} MiB (+{vram2 - vram1})")
logger.info(f"COMBINED models delta: {vram2 - vram0} MiB | board total in use: {vram2} MiB")


async def bot(runner_args: RunnerArguments) -> None:
    """Runner entry point — one call per client session."""
    assert isinstance(runner_args, SmallWebRTCRunnerArguments), "webrtc transport only"

    transport = SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_10ms_chunks=2,
        ),
    )
    # Tuned vs pipecat defaults (conf .7 / start .2 / stop .2 / min_vol .6):
    #  - stop_secs 0.8: natural mid-sentence pauses no longer split the turn
    #    (default 0.2 fragmented long sentences into many segments, and each
    #    new segment's speech barged-in on the previous reply — the user only ever
    #    heard the tail). Costs ~0.6s extra before the reply starts.
    #  - min_volume 0.35 + confidence 0.6 + start_secs 0.15: faster, more
    #    sensitive barge-in trigger for a quiet headset mic (was ~2s of talking).
    vad_params = VADParams(confidence=0.6, start_secs=0.15, stop_secs=0.8, min_volume=0.35)
    pipeline = Pipeline([
        transport.input(),
        VADProcessor(vad_analyzer=SileroVADAnalyzer(params=vad_params)),
        BargeIn(),
        SharedWhisperSTTService(model="large-v3-turbo", device="cuda", compute_type="float16"),
        Parrot(),
        ChatterboxTurboTTSService(CHATTERBOX, REF_VOICE),
        transport.output(),
    ])
    task = PipelineTask(pipeline)

    @transport.event_handler("on_client_connected")
    async def on_connected(t, c):
        logger.info("client connected — echo loop live")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(t, c):
        logger.info("client disconnected — stopping pipeline")
        await task.cancel()

    await PipelineRunner().run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
