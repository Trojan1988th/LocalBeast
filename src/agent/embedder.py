"""
embedder.py — Ollama embedding wrapper for the Knowledge RAG system.

Single public function: embed(text) -> list[float] | None
Returns None (not an exception) when Ollama is unavailable —
callers degrade gracefully to keyword-only search.

Model: rjmalagon/gte-qwen2-7b-instruct (3584 dims)
       Install: ollama pull rjmalagon/gte-qwen2-7b-instruct
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=True)

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "rjmalagon/gte-qwen2-7b-instruct")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "http://localhost:11434")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIMENSIONS", "3584"))

# Thread-local Ollama client
_local = threading.local()

# Cached availability flag — rechecked if None
_ollama_available: bool | None = None
_availability_lock = threading.Lock()


def _get_client():
    """Get (or create) a thread-local Ollama client."""
    if not hasattr(_local, "client"):
        import ollama
        _local.client = ollama.Client(host=EMBEDDING_BASE_URL)
    return _local.client


def _check_availability() -> bool:
    """Check if Ollama is reachable. Caches result until reset."""
    global _ollama_available
    with _availability_lock:
        if _ollama_available is not None:
            return _ollama_available
        try:
            client = _get_client()
            client.list()  # lightweight ping
            _ollama_available = True
            logger.info("Ollama available at %s (model: %s)", EMBEDDING_BASE_URL, EMBEDDING_MODEL)
        except Exception as e:
            _ollama_available = False
            logger.warning(
                "Ollama not available at %s: %s. Semantic search disabled.",
                EMBEDDING_BASE_URL, e,
            )
        return _ollama_available


def reset_availability_cache() -> None:
    """Force re-check of Ollama availability on next embed() call."""
    global _ollama_available
    with _availability_lock:
        _ollama_available = None


def is_available() -> bool:
    """Return True if Ollama is reachable."""
    return _check_availability()


def embed(text: str) -> list[float] | None:
    """
    Embed text using the configured Ollama model.

    Returns a list of EMBEDDING_DIM floats, or None if Ollama is unavailable.
    Never raises — callers should treat None as "skip semantic search".

    The section_header should be prepended to content before calling:
        embed(f"{section_header}\\n\\n{content}")
    """
    if not text or not text.strip():
        return None

    if not _check_availability():
        return None

    try:
        client = _get_client()
        # keep_alive=-1 keeps model resident in GPU; avoids ~11s reload when evicted
        response = client.embeddings(
            model=EMBEDDING_MODEL,
            prompt=text.strip(),
            keep_alive=-1,
        )
        vec = response["embedding"]
        if len(vec) != EMBEDDING_DIM:
            logger.warning(
                "Embedding dim mismatch: expected %d, got %d",
                EMBEDDING_DIM, len(vec),
            )
        return vec
    except Exception as e:
        # Mark unavailable so we stop hitting a broken Ollama
        global _ollama_available
        with _availability_lock:
            _ollama_available = False
        logger.warning("Embedding failed (Ollama disabled until restart): %s", e)
        return None


def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """
    Embed a list of texts. Returns a parallel list of vectors (None on failure).

    Ollama doesn't have a native batch endpoint, so this loops with a small
    delay between calls. Use for the background embedding job only.
    """
    import time

    if not _check_availability():
        return [None] * len(texts)

    results = []
    for text in texts:
        results.append(embed(text))
        time.sleep(0.05)  # 50ms between calls to avoid saturating Ollama
    return results
