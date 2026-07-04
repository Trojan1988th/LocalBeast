"""SSE streaming chat route for realtime voice mode.

POST /api/chat/stream — Server-Sent Events. Reuses graph.chat() UNCHANGED, so
prompt building, timestamp injection, auto-recall, 429 provider failover,
checkpoint clearing, end-of-turn persistence and Hindsight retention all behave
exactly like the blocking /api/chat route. Token streaming is achieved purely
from the outside:

  1. A dedicated "voice agent" is built with the same fallback LLM stack and
     tools, but with `streaming=True` on the inner ChatOpenAI/ChatKimi models —
     that makes LangChain fire `on_llm_new_token` callbacks *during* a normal
     synchronous `invoke()` (BaseChatOpenAI._generate delegates to _stream when
     streaming is enabled, passing tokens to the run manager).
  2. chat() already accepts a `config` dict which it forwards to agent.invoke();
     we pass our callback handler through it. No graph.py changes.
  3. `voice_mode` uses chat()'s existing `ephemeral_context` parameter (prompt-
     only injection, never persisted) to add the speech-shaping addendum.

Tool-call chatter never reaches the stream: tool-call argument deltas and Kimi
reasoning_content have empty `chunk.text`, so `on_llm_new_token` gets "" for
them and we drop empties. Content the model speaks BEFORE calling a tool (e.g.
"Let me check that...") IS streamed — deliberate: in voice mode that is exactly
the acknowledgment we want spoken. An `event: boundary` is emitted between LLM
rounds so the client can flush its sentence buffer.

SSE protocol:
  event: token     data: {"t": "<token text>"}
  event: boundary  data: {}                      (end of one LLM round)
  event: done      data: {"text": "<final assistant text>"}
  event: error     data: {"message": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel

from .graph import (
    CORE_MEMORY_TOOLS,
    LAST_ACTIVE_PATH,
    ChatKimi,
    LLMWithFallback,
    _build_core_memory_prompt,
    _build_llm_for_config,
    _get_llm_configs,
    _is_rate_limit_error,
    chat,
    create_react_agent,
    get_checkpointer,
)

logger = logging.getLogger("agent.voice_stream")

# Voice-mode addendum: injected per-turn via chat()'s ephemeral_context —
# prompt-only, never persisted to thread history. Override the wording via
# VOICE_MODE_ADDENDUM in .env to give it your own texture.
VOICE_MODE_ADDENDUM = os.environ.get("VOICE_MODE_ADDENDUM") or (
    "[Voice mode] The user is talking with you out loud right now — your reply will "
    "be spoken aloud in your voice by TTS. Be fully yourself: same warmth, same "
    "humor, same curiosity and honesty you always have. Nothing about who you are "
    "changes; only the medium does. Speak the way you would in a real conversation — "
    "usually a few natural sentences, but let the moment set the length: a quick "
    "answer can be three words, a story they asked for can breathe. Contractions, "
    "asides, trailing thoughts, and reacting to what they said are all natural here. "
    "You may use [chuckle] or [laugh] sparingly, only where you'd actually laugh. "
    "The mechanical rules, because a voice reads every character: no markdown of any "
    "kind — no bullets, asterisks, headers, code blocks, or emoji — and write "
    "numbers, dates, and abbreviations the way you'd say them aloud. If you're "
    "about to use a tool or check memory and it might take a moment, first say "
    "one short natural sentence about what you're doing — as yourself, not as a "
    "status message."
)

_voice_agent = None


class _StreamingChatKimi(ChatKimi):
    """ChatKimi + reasoning_content extraction on the STREAMING path.

    ChatKimi round-trips Kimi's reasoning_content via _create_chat_result, but
    that only runs on the non-streaming path. This langchain-openai version does
    not extract provider-specific delta fields from streamed chunks, so without
    this hook a streamed assistant tool-call message would lack reasoning_content
    and the next round would 400 ("reasoning_content is missing"). additional_kwargs
    string values concatenate on chunk merge, so per-delta pieces accumulate.
    """

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        gen = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        try:
            rc = chunk["choices"][0]["delta"].get("reasoning_content")
            if rc and gen is not None:
                gen.message.additional_kwargs["reasoning_content"] = rc
        except (KeyError, IndexError, TypeError):
            pass
        return gen


class _StreamingLLMWithFallback(LLMWithFallback):
    """LLMWithFallback whose streaming path has the same 429/503 failover.

    langchain-core routes invoke() through self._stream() when `streaming=True`
    is set on THIS instance (see BaseChatModel._should_stream). The parent only
    overrides _generate, so we mirror its failover here. Caveat: if a provider
    dies mid-stream (after yielding tokens), we re-raise rather than failover —
    retrying would duplicate already-emitted tokens. 429s happen at request
    start, so the failover still covers the real case.
    """

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        last_error = None
        for i, llm in enumerate(self._llms):
            emitted = False
            try:
                for chunk in llm._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
                    emitted = True
                    yield chunk
                return
            except Exception as e:
                if not emitted and _is_rate_limit_error(e) and i < len(self._llms) - 1:
                    last_error = e
                    logger.warning("voice stream: provider %d unavailable (%s), trying backup", i + 1, e)
                    continue
                raise
        if last_error:
            raise last_error


# M5: disable Kimi's thinking phase for voice turns (config-driven, revert by
# setting VOICE_DISABLE_THINKING=false in .env). Probe findings (2026-07-03):
# kimi-for-coding accepts {"thinking": {"type": "disabled"}} but then requires
# temperature=0.6 (400s on the usual temperature=1). Applied ONLY to the primary
# ChatKimi — the synthetic.new backup may not accept the param, and failover
# correctness beats failover speed. Normal chat routes are untouched (this
# module builds its own agent).
VOICE_DISABLE_THINKING = os.environ.get("VOICE_DISABLE_THINKING", "true").strip().lower() != "false"


def _streamify(llm):
    """Swap a ChatKimi instance to _StreamingChatKimi (adds no fields, so the
    class swap is safe) so streamed chunks keep reasoning_content, and apply
    the voice-only thinking override."""
    if type(llm) is ChatKimi:
        llm.__class__ = _StreamingChatKimi
        if VOICE_DISABLE_THINKING:
            llm.extra_body = {**(llm.extra_body or {}), "thinking": {"type": "disabled"}}
            llm.temperature = 0.6
            logger.info("voice agent: Kimi thinking DISABLED (temperature 0.6)")
    return llm


def _build_voice_agent():
    """Same stack as graph.build_agent(), but streaming-capable."""
    configs = _get_llm_configs()
    if not configs:
        raise ValueError("OPENAI_API_KEY is required (see .env)")
    if len(configs) > 1:
        llm = _StreamingLLMWithFallback(configs)
        llm._llms = [_streamify(inner) for inner in llm._llms]
    else:
        api_key, base_url, model = configs[0]
        llm = _streamify(_build_llm_for_config(api_key, base_url, model))
    # Triggers BaseChatModel._should_stream -> self._stream() during invoke(),
    # which fires on_llm_new_token per token to our callback handler.
    llm.streaming = True
    return create_react_agent(
        llm,
        tools=CORE_MEMORY_TOOLS,
        prompt=_build_core_memory_prompt,
        checkpointer=get_checkpointer(),
    )


def _get_voice_agent():
    global _voice_agent
    if _voice_agent is None:
        logger.info("Building voice agent (streaming LLMs)")
        _voice_agent = _build_voice_agent()
    return _voice_agent


class _TokenStreamHandler(BaseCallbackHandler):
    """Bridges sync LangChain token callbacks (worker thread) to an asyncio queue.

    Also instruments the turn: the first on_llm_start marks the end of the
    pre-LLM pipeline (history load + Hindsight auto-recall + prompt build),
    which chat() runs before ever calling Kimi.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self._loop = loop
        self._queue = queue
        self._t0 = time.perf_counter()
        self._llm_started = False

    def _put(self, item: tuple[str, Any]) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    def on_llm_start(self, serialized, prompts, **kwargs) -> None:
        if not self._llm_started:
            self._llm_started = True
            self._put(("pre_llm", time.perf_counter() - self._t0))

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.on_llm_start(serialized, messages, **kwargs)

    def on_llm_new_token(self, token: str, **kwargs) -> None:
        if token:  # tool-call deltas / reasoning chunks arrive as "" — drop
            self._put(("token", token))

    def on_llm_end(self, response, **kwargs) -> None:
        self._put(("boundary", None))


class ChatStreamRequest(BaseModel):
    # History: channel_type was briefly "internal" (Option C) to skip Hindsight
    # auto-recall when it cost 5-11s. After the TEI GPU reranker (2026-07-03)
    # recall runs ~1-2s, so auto-recall is back on ("local") — worth it
    # for conversational quality. This also makes use_knowledge functional again.
    message: str
    thread_id: str = "voice"
    user_id: str | None = None
    channel_type: str | None = "local"
    voice_mode: bool = False
    use_knowledge: bool = False
    is_private: bool = False
    # O3: multimodal voice turns. Verified 2026-07-03: kimi-for-coding accepts
    # image content on the streaming path (K2.5 MoonViT vision).
    image_data_urls: list[str] | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── O3: armed images ("look at this") ─────────────────────────────────
# The overlay captures a screenshot while voice is connected and ARMS it here;
# the next voice turn (next /chat/stream call) attaches and consumes it. The
# WebRTC data channel is far too small for base64 images (~1KB frames), so the
# overlay POSTs them to this holder instead. Armed images expire after 15s.
_ARM_TTL_S = 15.0
_armed_images: dict = {"images": None, "expires_at": 0.0, "consumed_at": 0.0}


class ArmImageRequest(BaseModel):
    image_data_urls: list[str]


def _take_armed_images() -> list[str] | None:
    if _armed_images["images"] and time.time() < _armed_images["expires_at"]:
        images = _armed_images["images"]
        _armed_images["images"] = None
        _armed_images["consumed_at"] = time.time()
        return images
    return None


def register_voice_stream_route(router: APIRouter) -> None:
    """Register POST /chat/stream + the arm-image endpoints (mounted under /api)."""

    @router.post("/voice/arm-image")
    async def arm_image(req: ArmImageRequest):
        _armed_images["images"] = req.image_data_urls[:5]
        _armed_images["expires_at"] = time.time() + _ARM_TTL_S
        _armed_images["consumed_at"] = 0.0
        logger.info("voice: %d image(s) armed for the next voice turn (%.0fs TTL)",
                    len(req.image_data_urls), _ARM_TTL_S)
        return {"armed": True, "ttl_s": _ARM_TTL_S}

    @router.get("/voice/arm-image")
    async def arm_image_status():
        now = time.time()
        if _armed_images["images"] and now < _armed_images["expires_at"]:
            return {"state": "armed", "remaining_s": round(_armed_images["expires_at"] - now, 1)}
        if _armed_images["consumed_at"] and now - _armed_images["consumed_at"] < 10:
            return {"state": "consumed"}
        return {"state": "none"}

    @router.post("/chat/stream")
    async def post_chat_stream(req: ChatStreamRequest):
        preview = req.message[:120].replace("\n", " ")
        logger.info(
            "POST /chat/stream thread=%s voice_mode=%s use_knowledge=%s msg=%r",
            req.thread_id, req.voice_mode, req.use_knowledge, preview,
        )
        # channel_type="internal" skips chat()'s last-active tracking, but a voice
        # turn IS the user actively chatting — write it here so heartbeats still skip.
        try:
            LAST_ACTIVE_PATH.write_text(str(time.time()))
        except Exception:
            pass

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        handler = _TokenStreamHandler(loop, queue)

        # O3: attach images — explicit on the request, else any armed-and-fresh
        # screenshot from the overlay's "look at this" gesture.
        images = req.image_data_urls or _take_armed_images()
        if images:
            logger.info("voice turn carries %d image(s)", len(images))

        chat_task = asyncio.create_task(asyncio.to_thread(
            chat,
            _get_voice_agent(),
            req.thread_id,
            req.message,
            user_display_name=os.environ.get("USER_DISPLAY_NAME", "User"),
            config={"callbacks": [handler]},
            user_id=req.user_id,
            channel_type=req.channel_type or "local",
            channel_mode="admin",
            is_private_session=req.is_private,
            use_knowledge=req.use_knowledge,
            image_data_urls=images,
            ephemeral_context=VOICE_MODE_ADDENDUM if req.voice_mode else None,
        ))

        t0 = time.perf_counter()

        async def gen():
            timings: dict = {}
            try:
                while True:
                    if chat_task.done() and queue.empty():
                        break
                    try:
                        kind, data = await asyncio.wait_for(queue.get(), timeout=0.25)
                    except asyncio.TimeoutError:
                        continue
                    if kind == "token":
                        if "first_token_s" not in timings:
                            timings["first_token_s"] = round(time.perf_counter() - t0, 2)
                        yield _sse("token", {"t": data})
                    elif kind == "boundary":
                        yield _sse("boundary", {})
                    elif kind == "pre_llm":
                        timings["pre_llm_s"] = round(data, 2)
                try:
                    result = await chat_task
                    final = result.get("last_ai_content") or ""
                    timings["total_s"] = round(time.perf_counter() - t0, 2)
                    logger.info(
                        "voice turn timings: pre_llm=%ss first_token=%ss total=%ss",
                        timings.get("pre_llm_s"), timings.get("first_token_s"), timings.get("total_s"),
                    )
                    yield _sse("done", {"text": final, "timings": timings})
                except RuntimeError as e:
                    # chat()'s curated 401/429 messages
                    logger.error("chat/stream RuntimeError: %s", e)
                    yield _sse("error", {"message": str(e)})
                except Exception as e:
                    logger.exception("chat/stream failed")
                    yield _sse("error", {"message": f"Agent error: {e}"})
            finally:
                # Client disconnected mid-stream (e.g. barge-in): let chat() finish
                # in its thread so persistence + Hindsight retention still complete.
                if not chat_task.done():
                    logger.info("chat/stream client gone — chat() continues to persist turn")

        return StreamingResponse(gen(), media_type="text/event-stream")
