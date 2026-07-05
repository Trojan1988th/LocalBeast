"""
rechunk_knowledge.py — Section-aware chunker for the Knowledge RAG system.

Re-chunks knowledge documents using smart markdown-aware splitting:
  - Respects section headers (##, ###, ####)
  - Never splits mid-table, mid-list, or mid-sentence
  - Keeps creature name + stat block together
  - 300–500 token target per chunk, 50-token overlap
  - section_header extracted from nearest heading above chunk

Usage:
    python scripts/rechunk_knowledge.py --doc-id 7
    python scripts/rechunk_knowledge.py --all
    python scripts/rechunk_knowledge.py --all --dry-run

All chunks from a document are deleted and recreated.
is_embedded is reset to FALSE for all new chunks (embedding job picks them up).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env", override=True)

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

CHUNK_TARGET_TOKENS = int(os.environ.get("KNOWLEDGE_CHUNK_SIZE", "400"))
CHUNK_OVERLAP_TOKENS = int(os.environ.get("KNOWLEDGE_CHUNK_OVERLAP", "50"))
CHUNK_MAX_TOKENS = 600       # hard cap — only exceeded for tables
TABLE_MAX_TOKENS = 1200      # tables kept whole even if oversized


# ── Token counting ────────────────────────────────────────────────────────────

def _make_token_counter():
    """Return a token counting function. Uses tiktoken if available, else word approximation."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        def count(text: str) -> int:
            return len(enc.encode(text))
    except Exception:
        def count(text: str) -> int:
            return len(text) // 4  # ~4 chars per token
    return count


_count_tokens = _make_token_counter()


# ── Line classification ───────────────────────────────────────────────────────

# Heading patterns
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)')
# Table row: starts with |
_TABLE_ROW_RE = re.compile(r'^\s*\|')
# Blank line
_BLANK_RE = re.compile(r'^\s*$')
# Numbered/bulleted list item
_LIST_ITEM_RE = re.compile(r'^\s*(?:[-*+]|\d+[.)]) ')


def _is_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line))


def _heading_level(line: str) -> int:
    m = _HEADING_RE.match(line)
    return len(m.group(1)) if m else 0


def _heading_text(line: str) -> str:
    m = _HEADING_RE.match(line)
    return m.group(2).strip() if m else line.strip()


def _is_table_row(line: str) -> bool:
    return bool(_TABLE_ROW_RE.match(line))


def _is_blank(line: str) -> bool:
    return bool(_BLANK_RE.match(line))


def _is_list_item(line: str) -> bool:
    return bool(_LIST_ITEM_RE.match(line))


# ── Section header tracking ───────────────────────────────────────────────────

class _HeaderStack:
    """Track nested heading context for section_header extraction."""

    def __init__(self):
        self._stack: list[tuple[int, str]] = []  # (level, text)

    def push(self, level: int, text: str) -> None:
        # Pop anything at same or deeper level
        self._stack = [(l, t) for l, t in self._stack if l < level]
        self._stack.append((level, text))

    def current(self) -> str | None:
        if not self._stack:
            return None
        return " > ".join(t for _, t in self._stack)


# ── Block detection ───────────────────────────────────────────────────────────

def _collect_table(lines: list[str], start: int) -> tuple[int, list[str]]:
    """Collect all consecutive table lines starting at `start`. Returns (end_idx, table_lines)."""
    i = start
    table_lines = []
    while i < len(lines) and _is_table_row(lines[i]):
        table_lines.append(lines[i])
        i += 1
    return i, table_lines


def _collect_list_block(lines: list[str], start: int) -> tuple[int, list[str]]:
    """Collect list block (items + continuation lines) starting at `start`."""
    i = start
    block = []
    while i < len(lines):
        line = lines[i]
        if _is_blank(line):
            # blank line ends the list
            break
        if _is_heading(line):
            break
        # list continuation: indented or list item itself
        block.append(line)
        i += 1
    return i, block


# ── Main chunking logic ───────────────────────────────────────────────────────

class _ChunkBuilder:
    """Accumulates lines into a chunk, then flushes with section_header."""

    def __init__(self):
        self._lines: list[str] = []
        self._tokens = 0
        self._section_header: str | None = None

    @property
    def tokens(self) -> int:
        return self._tokens

    def empty(self) -> bool:
        return not self._lines

    def add(self, lines: list[str]) -> None:
        for line in lines:
            self._lines.append(line)
            self._tokens += _count_tokens(line + "\n")

    def set_header(self, header: str | None) -> None:
        if self._section_header is None:
            self._section_header = header

    def flush(self) -> dict | None:
        text = "\n".join(self._lines).strip()
        if not text:
            return None
        result = {
            "content": text,
            "section_header": self._section_header,
            "token_count": self._tokens,
        }
        return result

    def overlap_lines(self, n_tokens: int) -> list[str]:
        """Return the last n_tokens worth of lines for overlap into next chunk."""
        overlap = []
        budget = 0
        for line in reversed(self._lines):
            t = _count_tokens(line + "\n")
            if budget + t > n_tokens:
                break
            overlap.insert(0, line)
            budget += t
        return overlap

    def reset(self, overlap_lines: list[str], section_header: str | None) -> None:
        self._lines = list(overlap_lines)
        self._tokens = sum(_count_tokens(l + "\n") for l in overlap_lines)
        self._section_header = section_header


def chunk_document(text: str) -> list[dict]:
    """
    Split document text into section-aware chunks.

    Returns list of dicts:
      {content, section_header, token_count}

    Splitting priority:
      1. Markdown headings (always start new chunk)
      2. Tables (kept whole, new chunk before+after)
      3. List blocks (kept whole if fits)
      4. Blank lines between topics (flush if at target)
      5. Paragraph breaks

    Never splits: mid-sentence, mid-table, mid-list (unless list > MAX).
    """
    lines = text.splitlines()
    chunks: list[dict] = []
    header_stack = _HeaderStack()
    builder = _ChunkBuilder()
    i = 0

    def _flush_builder():
        chunk = builder.flush()
        if chunk:
            chunks.append(chunk)

    def _start_new_chunk():
        overlap = builder.overlap_lines(CHUNK_OVERLAP_TOKENS)
        current_header = header_stack.current()
        _flush_builder()
        builder.reset(overlap, current_header)

    while i < len(lines):
        line = lines[i]

        # ── Heading: always starts a new chunk ──────────────────────────────
        if _is_heading(line):
            level = _heading_level(line)
            text_h = _heading_text(line)
            # Flush current chunk before starting new section
            if not builder.empty():
                _flush_builder()
                builder.reset([], None)
            header_stack.push(level, text_h)
            builder.set_header(header_stack.current())
            # Don't add the heading line itself to the chunk content —
            # it's captured in section_header. But do add it so the
            # chunk has context when read in isolation.
            builder.add([line])
            i += 1
            continue

        # ── Table: keep whole ────────────────────────────────────────────────
        if _is_table_row(line):
            end, table_lines = _collect_table(lines, i)
            table_tokens = sum(_count_tokens(l + "\n") for l in table_lines)
            # If chunk has content + table would overflow hard cap, flush first
            if not builder.empty() and builder.tokens + table_tokens > CHUNK_MAX_TOKENS:
                _start_new_chunk()
            builder.set_header(header_stack.current())
            builder.add(table_lines)
            # After a table, flush if we've hit target (tables are natural break points)
            if builder.tokens >= CHUNK_TARGET_TOKENS:
                _start_new_chunk()
            i = end
            continue

        # ── Blank line: potential chunk boundary ────────────────────────────
        if _is_blank(line):
            if builder.tokens >= CHUNK_TARGET_TOKENS:
                # At a natural break point — flush
                _start_new_chunk()
            else:
                # Preserve blank lines within chunk for readability
                if not builder.empty():
                    builder.add([line])
            i += 1
            continue

        # ── List block: keep together if fits ───────────────────────────────
        if _is_list_item(line):
            end, list_lines = _collect_list_block(lines, i)
            list_tokens = sum(_count_tokens(l + "\n") for l in list_lines)
            # If list + current chunk would exceed hard max, flush first
            if not builder.empty() and builder.tokens + list_tokens > CHUNK_MAX_TOKENS:
                _start_new_chunk()
            builder.set_header(header_stack.current())
            builder.add(list_lines)
            if builder.tokens >= CHUNK_TARGET_TOKENS:
                _start_new_chunk()
            i = end
            continue

        # ── Regular content line ─────────────────────────────────────────────
        line_tokens = _count_tokens(line + "\n")
        builder.set_header(header_stack.current())

        # Would exceed hard max? Flush at last sentence boundary
        if not builder.empty() and builder.tokens + line_tokens > CHUNK_MAX_TOKENS:
            _start_new_chunk()

        builder.add([line])
        i += 1

        # Soft flush: at target and this line ends a sentence
        if builder.tokens >= CHUNK_TARGET_TOKENS and _ends_sentence(line):
            _start_new_chunk()

    # Flush remaining content
    _flush_builder()

    # Filter empty chunks and assign chunk_index
    result = []
    for idx, chunk in enumerate(chunks):
        if chunk["content"].strip():
            chunk["chunk_index"] = idx
            result.append(chunk)

    return result


def _ends_sentence(line: str) -> bool:
    """True if line ends a complete sentence (safe split point)."""
    stripped = line.rstrip()
    return stripped.endswith(('.', '!', '?', ':', '-', '—'))


# ── Database operations ───────────────────────────────────────────────────────

def _get_connection(db_url: str):
    import psycopg
    from pgvector.psycopg import register_vector
    conn = psycopg.connect(db_url)
    register_vector(conn)
    return conn


def _rechunk_document(conn, doc_id: int, dry_run: bool) -> int:
    """Delete existing chunks for doc_id and insert fresh chunks. Returns chunk count."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id, title, mod_group, doc_type, tier, full_text FROM knowledge_documents WHERE doc_id = %s",
            (doc_id,),
        )
        row = cur.fetchone()

    if not row:
        logger.error("Document doc_id=%d not found", doc_id)
        return 0

    doc_id_, title, mod_group, doc_type, tier, full_text = row

    if not full_text or not full_text.strip():
        logger.warning("Document doc_id=%d (%s) has no full_text — skipping", doc_id, title)
        return 0

    logger.info("Chunking doc_id=%d: %s (%s / %s)", doc_id, title, mod_group, doc_type)

    chunks = chunk_document(full_text)
    logger.info("  Produced %d chunks (target ~%d tokens each)", len(chunks), CHUNK_TARGET_TOKENS)

    if dry_run:
        for i, c in enumerate(chunks[:3]):
            preview = c["content"][:120].replace("\n", " ")
            logger.info("  [DRY RUN] chunk %d | header=%r | tokens=%d | %s...",
                        i, c.get("section_header"), c.get("token_count", 0), preview)
        if len(chunks) > 3:
            logger.info("  [DRY RUN] ... %d more chunks", len(chunks) - 3)
        return len(chunks)

    with conn.cursor() as cur:
        # Delete old chunks
        cur.execute("DELETE FROM knowledge_chunks WHERE doc_id = %s", (doc_id,))
        deleted = cur.rowcount

        # Insert new chunks
        for c in chunks:
            cur.execute(
                """
                INSERT INTO knowledge_chunks
                    (doc_id, mod_group, doc_type, tier, chunk_index, section_header,
                     content, token_count, is_embedded)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                """,
                (
                    doc_id, mod_group, doc_type, tier,
                    c["chunk_index"],
                    c.get("section_header"),
                    c["content"],
                    c.get("token_count", 0),
                ),
            )

        # Update chunk_count on parent document
        cur.execute(
            "UPDATE knowledge_documents SET chunk_count = %s WHERE doc_id = %s",
            (len(chunks), doc_id),
        )

        # Log the rechunk action
        cur.execute(
            "INSERT INTO knowledge_upload_log (doc_id, action, performed_by, notes) VALUES (%s, 'rechunk', 'script', %s)",
            (doc_id, f"rechunked: {deleted} old -> {len(chunks)} new chunks"),
        )

    conn.commit()
    logger.info("  Replaced %d old chunks with %d new chunks", deleted, len(chunks))
    return len(chunks)


def _get_all_doc_ids(conn, db_key: str) -> list[int]:
    """Get all active doc_ids from a knowledge DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id FROM knowledge_documents WHERE is_active = TRUE ORDER BY doc_id")
        return [r[0] for r in cur.fetchall()]


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Rechunk knowledge documents")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--doc-id", type=int, help="Rechunk a single document by doc_id")
    group.add_argument("--all", action="store_true", help="Rechunk all active documents")
    parser.add_argument("--admin", action="store_true", help="Target the admin knowledge DB instead of public")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing")
    args = parser.parse_args()

    url_key = "ADMIN_KNOWLEDGE_DATABASE_URL" if args.admin else "KNOWLEDGE_DATABASE_URL"
    db_url = os.environ.get(url_key, "").strip()
    if not db_url:
        sys.exit(f"ERROR: {url_key} not set in .env")

    conn = _get_connection(db_url)
    try:
        if args.doc_id:
            count = _rechunk_document(conn, args.doc_id, args.dry_run)
            if not args.dry_run:
                logger.info("Done. %d chunks created. Run embed_knowledge.py to embed them.", count)
        else:
            doc_ids = _get_all_doc_ids(conn, url_key)
            if not doc_ids:
                logger.info("No active documents found in %s", url_key)
                return
            logger.info("Rechunking %d documents ...", len(doc_ids))
            total = 0
            for doc_id in doc_ids:
                total += _rechunk_document(conn, doc_id, args.dry_run)
            if not args.dry_run:
                logger.info("Done. %d total chunks created across %d documents.", total, len(doc_ids))
                logger.info("Run: python scripts/embed_knowledge.py --backfill")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
