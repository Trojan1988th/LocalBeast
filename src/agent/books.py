r"""Books (B1): public-domain audiobook library — import, chapters, text-prep.

Design (BOOKS_RECON.md, approved at B0 review):
- Storage: data/books\ (env: BOOKS_ROOT) — self-contained media library.
    books\<book-id>\source.txt        original text (post format-extraction)
    books\<book-id>\chapters\NNN.txt  PREPARED render text, one file per chapter
    library.db                        SQLite index (stdlib sqlite3, no deps)
- Formats: Project Gutenberg plain-text (.txt) and .epub (ebooklib — a
  maintained parser, not hand-rolled). DRM'd epubs are detected
  (META-INF/encryption.xml) and DECLINED with a clear message — no
  circumvention features, ever. Public domain / DRM-free only; the UI says so.
- Chapter detection is heuristic (CHAPTER I / Roman numerals / numbered
  headings); the review screen (rename / merge / split) is the fallback.
- Text-prep runs at IMPORT time (inspectable per chapter, cache keys stay
  stable): Gutenberg boilerplate stripped, _underscores_ → plain words,
  footnote markers removed, ALL-CAPS calmed, asterisk scene breaks → a
  [[scene-break]] sentinel the renderer turns into a pause (never spoken),
  chapter headings stored for "Chapter Three." announcements at render time.

All routes are registered via register_books_routes(router) — api.py touch
stays two lines, mirroring voice_stream.py.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger("agent.books")

BOOKS_ROOT = Path(os.environ.get("BOOKS_ROOT", "data/books"))
SCENE_BREAK = "[[scene-break]]"  # sentinel: renderer inserts a pause, never spoken

READER_BASE = os.environ.get("READER_BASE", "http://127.0.0.1:5005")
# Cache key includes engine + dials so voice/dial changes invalidate naturally
# (BOOKS_BUILD B3 contract, established here in B2). Keep in sync with the
# reader service delivery-consistency defaults.
DIALS_KEY = os.environ.get("BOOKS_DIALS_KEY", "orpheus|t0.6|p0.9|r1.1")
MIN_UNIT_CHARS = 120  # short-dialogue paragraphs merge into render units (recon flag)
# B3 retune (the user's first-listen finding): long units are where drift lives —
# misreads and spontaneous character voices cluster in big merged spans.
MAX_UNIT_CHARS = int(os.environ.get("BOOKS_MAX_UNIT_CHARS", "420"))

# V2 (2026-07-05, the user's verdict): the character-voice theater is RETIRED.
# Voice-consistency mode is the default — consistent the agent, no spontaneous
# casting. Three levers, each measured:
#   - cooler book temperature (theater lives in the sampling heat),
#   - a tighter unit ceiling through dialogue-heavy passages (long dialogue
#     runs are where the actor starts casting),
#   - a conservative speaker-similarity retake gate in the reader service
#     (catches female-persona drift; male-timbre overlap made a stricter
#     threshold false-reject real takes — see DECISIONS).
# A per-book `theater` toggle (default OFF) keeps the old behavior reachable.
BOOK_TEMPERATURE = float(os.environ.get("READER_BOOK_TEMPERATURE", "0.45"))
DIALOGUE_MAX_UNIT_CHARS = int(os.environ.get("BOOKS_DIALOGUE_MAX_UNIT_CHARS", "260"))
_DIALOGUE_QUOTE_MIN = 6  # quote marks in a merged span before the tight ceiling applies
CONSISTENCY_DIALS_KEY = os.environ.get(
    "BOOKS_CONSISTENCY_DIALS_KEY", f"orpheus|t{BOOK_TEMPERATURE}|p0.9|r1.1|sim1")


def _dials_key_for(theater: bool) -> str:
    return DIALS_KEY if theater else CONSISTENCY_DIALS_KEY


def _dialogue_heavy(text: str) -> bool:
    return sum(text.count(q) for q in ('"', '“', '”')) >= _DIALOGUE_QUOTE_MIN
VERIFY_THRESHOLD_NOTE = 0.80  # actual threshold lives in the reader service
LAST_ACTIVE = Path(__file__).resolve().parents[2] / "data" / "last_active.txt"
VOICE_FLAG = Path(__file__).resolve().parents[2] / "data" / "voice_session_active"


# ── Storage ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    BOOKS_ROOT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(BOOKS_ROOT / "library.db")
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS books (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            source_filename TEXT DEFAULT '',
            format TEXT DEFAULT 'txt',
            status TEXT DEFAULT 'review',      -- review | ready
            voice TEXT DEFAULT '',             -- per-book voice (B2/B5)
            created_at REAL,
            prep_notes TEXT DEFAULT ''
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS positions (
            book_id TEXT PRIMARY KEY,
            chapter INTEGER, unit INTEGER, updated_at REAL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS bookmarks (
            id TEXT PRIMARY KEY, book_id TEXT, chapter INTEGER, unit INTEGER,
            name TEXT, created_at REAL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS highlights (
            id TEXT PRIMARY KEY, book_id TEXT, chapter INTEGER, unit INTEGER,
            text TEXT, created_at REAL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS unit_status (
            book_id TEXT, hash TEXT, chapter INTEGER, unit INTEGER,
            ratio REAL, attempts INTEGER, passed INTEGER, created_at REAL,
            PRIMARY KEY (book_id, hash)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS prerender (
            book_id TEXT, chapter INTEGER, status TEXT DEFAULT 'queued',
            queued_at REAL, started_at REAL, done_at REAL,
            PRIMARY KEY (book_id, chapter)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS chapters (
            book_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            title TEXT DEFAULT '',
            char_count INTEGER DEFAULT 0,
            para_count INTEGER DEFAULT 0,
            audio_seconds REAL DEFAULT 0,      -- duration-so-far (filled by B3 cache)
            PRIMARY KEY (book_id, idx)
        )"""
    )
    # V2 additive migration: per-book theater toggle (default OFF = consistent the agent)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(books)")}
    if "theater" not in cols:
        con.execute("ALTER TABLE books ADD COLUMN theater INTEGER DEFAULT 0")
    return con


def _book_dir(book_id: str) -> Path:
    return BOOKS_ROOT / "books" / book_id


def _chapter_path(book_id: str, idx: int) -> Path:
    return _book_dir(book_id) / "chapters" / f"{idx:03d}.txt"


def _write_chapters(book_id: str, chapters: list[dict]) -> None:
    """Persist chapter texts + index rows (full rewrite — B1 books are small text)."""
    ch_dir = _book_dir(book_id) / "chapters"
    ch_dir.mkdir(parents=True, exist_ok=True)
    for old in ch_dir.glob("*.txt"):
        old.unlink()
    con = _db()
    with con:
        con.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))
        for i, ch in enumerate(chapters):
            _chapter_path(book_id, i).write_text(ch["text"], encoding="utf-8")
            paras = [p for p in ch["text"].split("\n\n") if p.strip()]
            con.execute(
                "INSERT INTO chapters (book_id, idx, title, char_count, para_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (book_id, i, ch["title"], len(ch["text"]), len(paras)),
            )
    con.close()


def _read_chapters(book_id: str) -> list[dict]:
    con = _db()
    rows = con.execute(
        "SELECT idx, title FROM chapters WHERE book_id = ? ORDER BY idx", (book_id,)
    ).fetchall()
    con.close()
    return [
        {"idx": r["idx"], "title": r["title"],
         "text": _chapter_path(book_id, r["idx"]).read_text(encoding="utf-8")}
        for r in rows
    ]


# ── Format extraction ─────────────────────────────────────────────────────────

_GUT_START = re.compile(r"\*{3}\s*START OF (THE|THIS) PROJECT GUTENBERG EBOOK.*?\*{3}", re.I)
_GUT_END = re.compile(r"\*{3}\s*END OF (THE|THIS) PROJECT GUTENBERG EBOOK.*?\*{3}", re.I)


def _strip_gutenberg(text: str) -> str:
    m = _GUT_START.search(text)
    if m:
        text = text[m.end():]
    m = _GUT_END.search(text)
    if m:
        text = text[: m.start()]
    return text.strip()


def _extract_txt(raw: bytes) -> tuple[str, str, str]:
    """-> (text, title_guess, author_guess) from a Gutenberg-style .txt"""
    text = raw.decode("utf-8-sig", errors="replace")
    title = author = ""
    m = re.search(r"^Title:\s*(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    m = re.search(r"^Author:\s*(.+)$", text, re.M)
    if m:
        author = m.group(1).strip()
    return _strip_gutenberg(text), title, author


def _epub_is_drm(path: Path) -> bool:
    """Adobe/B&N DRM'd epubs carry META-INF/encryption.xml. Detect and decline
    (fonts-only obfuscation is rare in the wild; we decline conservatively)."""
    try:
        with zipfile.ZipFile(path) as z:
            return "META-INF/encryption.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def _extract_epub(path: Path) -> tuple[list[tuple[str, str]], str, str]:
    """-> ([(chapter_title, text)], title, author) via ebooklib.

    Chapters are split on heading TAGS across the whole spine — Gutenberg
    epubs pack many chapters into one spine document, so per-document
    splitting undercounts badly (P&P: 15 docs vs 61 chapters). Boilerplate
    chapters (Gutenberg header/license) are dropped by content match."""
    import warnings

    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    from ebooklib import epub, ITEM_DOCUMENT

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    book = epub.read_epub(str(path))
    title = (book.get_metadata("DC", "title") or [("", {})])[0][0] or ""
    author = (book.get_metadata("DC", "creator") or [("", {})])[0][0] or ""

    chapters: list[tuple[str, list[str]]] = []
    cur_title = ""
    cur: list[str] = []

    def flush():
        nonlocal cur, cur_title
        if cur:
            chapters.append((cur_title, cur))
        cur = []

    for item_id, _linear in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "lxml")
        for tag in soup(["sup", "style", "script"]):
            tag.decompose()  # sup = footnote markers in most epubs
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p"]):
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            if el.name.startswith("h"):
                flush()
                cur_title = text
            else:
                cur.append(text)
    flush()

    def is_boilerplate(t: str, body: str) -> bool:
        probe = (t + " " + body[:300]).upper()
        return "PROJECT GUTENBERG" in probe or "FULL LICENSE" in probe

    docs = [
        (t, "\n\n".join(paras))
        for t, paras in chapters
        if not is_boilerplate(t, "\n\n".join(paras))
    ]
    return docs, title, author


# ── Chapter detection (plain text) ────────────────────────────────────────────

_CHAPTER_RE = re.compile(
    r"^\s*(CHAPTER|Chapter|PART|Part|BOOK|Book)\s+([IVXLCDM]+|[0-9]+|[A-Z][a-z]+)\b\.?\s*(.*)$"
)
_ROMAN_LINE = re.compile(r"^\s*[IVXLCDM]{1,7}\.?\s*$")


def detect_chapters_txt(text: str) -> list[dict]:
    """Heuristic split on CHAPTER/PART headings; roman-numeral-only lines as a
    fallback. No headings found -> one chapter. The review screen is the net."""
    lines = text.split("\n")
    marks: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _CHAPTER_RE.match(line)
        if m:
            title = line.strip().rstrip(".")
            # Gutenberg often puts the chapter NAME on the same or next line
            if not m.group(3) and i + 1 < len(lines) and lines[i + 1].strip() \
                    and not _CHAPTER_RE.match(lines[i + 1]) and len(lines[i + 1].strip()) < 80:
                title = f"{title}. {lines[i + 1].strip()}"
            marks.append((i, title))
        elif _ROMAN_LINE.match(line) and not marks:
            marks.append((i, line.strip().rstrip(".")))
    if not marks:
        return [{"title": "Full text", "text": text.strip()}]
    chapters = []
    # Anything before the first heading (title page, dedication) is dropped from
    # render text but kept in source.txt.
    for n, (start, title) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        body_lines = lines[start + 1: end]
        body = "\n".join(body_lines).strip()
        if len(body) < 200 and n + 1 < len(marks):
            continue  # a bare heading in a table of contents — skip
        chapters.append({"title": title, "text": body})
    return chapters or [{"title": "Full text", "text": text.strip()}]


# ── Text-prep (import-time, inspectable) ─────────────────────────────────────

_FOOTNOTE = re.compile(r"\[\d{1,3}\]")
_BRACKET_JUNK = re.compile(r"\[\s*(Illustration|Copyright|Picture|Frontispiece)[^\]]*\]?", re.I)
_UNDERSCORE_EM = re.compile(r"_([^_\n]{1,200}?)_")
_SCENE_BREAK_LINE = re.compile(r"^\s*(\*\s*){3,}\s*$|^\s*(\*|#|~){1,5}\s*$")
_ALLCAPS_WORD = re.compile(r"\b[A-Z]{4,}\b")
_CAPS_WHITELIST = {"LLC", "USA", "HTML", "READ", "NOTE"}  # extend as found


def prep_text(text: str) -> str:
    """The render-facing text: everything a voice would misread, fixed here so
    cache keys stay stable and the result is inspectable per chapter."""
    out_lines: list[str] = []
    for raw_para in text.split("\n"):
        line = raw_para
        if _SCENE_BREAK_LINE.match(line):
            out_lines.append(SCENE_BREAK)
            continue
        line = _FOOTNOTE.sub("", line)
        line = _BRACKET_JUNK.sub("", line)  # illustration/copyright captions
        line = _UNDERSCORE_EM.sub(r"\1", line)  # _emphasis_ -> plain (the voice carries it)
        line = _ALLCAPS_WORD.sub(
            lambda m: m.group(0) if m.group(0) in _CAPS_WHITELIST else m.group(0).capitalize(),
            line,
        )
        out_lines.append(line)
    text = "\n".join(out_lines)
    # Re-flow: paragraphs separated by blank lines; single newlines inside a
    # paragraph (Gutenberg hard-wrap) become spaces.
    paras = re.split(r"\n\s*\n", text)
    flowed = []
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if p != SCENE_BREAK:
            p = re.sub(r"\s*\n\s*", " ", p)
            # Illustrated-edition drop caps extract as a detached capital:
            # "I T is a truth" -> "It is a truth" (paragraph start only).
            p = re.sub(r"^([A-Z])\s+([A-Z])\s+(?=[a-z])",
                       lambda m: m.group(1) + m.group(2).lower() + " ", p)
        flowed.append(p)
    return "\n\n".join(flowed)


# ── Import pipeline ───────────────────────────────────────────────────────────

def import_book(filename: str, raw: bytes, tmp_dir: Path) -> dict:
    ext = Path(filename).suffix.lower()
    book_id = uuid.uuid4().hex[:12]
    bdir = _book_dir(book_id)
    bdir.mkdir(parents=True, exist_ok=True)

    if ext == ".epub":
        tmp = tmp_dir / f"{book_id}.epub"
        tmp.write_bytes(raw)
        if _epub_is_drm(tmp):
            tmp.unlink()
            bdir.rmdir()
            raise HTTPException(
                status_code=422,
                detail="This epub is DRM-protected. Only DRM-free files can be "
                       "imported (this tool will never circumvent DRM).",
            )
        docs, title, author = _extract_epub(tmp)
        tmp.unlink()
        # Filter obvious non-chapters (cover/toc/colophon) by size
        docs = [(t, x) for t, x in docs if len(x) >= 400]
        chapters = [
            {"title": t or f"Chapter {n + 1}", "text": prep_text(x)}
            for n, (t, x) in enumerate(docs)
        ] or [{"title": "Full text", "text": ""}]
        source_text = "\n\n\n".join(x for _, x in docs)
        fmt = "epub"
    elif ext == ".txt":
        text, title, author = _extract_txt(raw)
        source_text = text
        chapters = [
            {"title": c["title"], "text": prep_text(c["text"])}
            for c in detect_chapters_txt(text)
        ]
        fmt = "txt"
    else:
        bdir.rmdir()
        raise HTTPException(status_code=422, detail=f"Unsupported format {ext!r} — .txt and .epub only")

    (bdir / "source.txt").write_text(source_text, encoding="utf-8")
    title = title or Path(filename).stem.replace("_", " ").title()
    con = _db()
    with con:
        con.execute(
            "INSERT INTO books (id, title, author, source_filename, format, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'review', ?)",
            (book_id, title, author, filename, fmt, time.time()),
        )
    con.close()
    _write_chapters(book_id, chapters)
    logger.info("books: imported %r (%s) — %d chapter(s)", title, fmt, len(chapters))
    return {"id": book_id, "title": title, "author": author, "chapters": len(chapters)}


# ── Render units + audio cache (B2) ──────────────────────────────────────────

def render_units(prepared_text: str, theater: bool = False) -> list[dict]:
    """Paragraphs -> render units. Short paragraphs (dialogue) merge forward
    into the previous unit — sub-~120-char targets trip Orpheus's early-exit
    quirk (recon flag). Scene breaks are pause units, never rendered.
    Each unit: {unit, text, paras: [para indices], pause: bool}.

    Consistency mode (theater=False, the default): dialogue-heavy spans use a
    TIGHTER ceiling — long dialogue runs are where the actor starts casting
    characters. Theater mode keeps the legacy single ceiling."""
    paras = [p for p in prepared_text.split("\n\n") if p.strip()]
    units: list[dict] = []
    for i, p in enumerate(paras):
        if p == SCENE_BREAK:
            units.append({"text": "", "paras": [i], "pause": True})
            continue
        prev = units[-1] if units and not units[-1]["pause"] else None
        wants_merge = prev is not None and (len(prev["text"]) < MIN_UNIT_CHARS or len(p) < MIN_UNIT_CHARS)
        # CEILING: never merge past the ceiling — long units drift (misreads,
        # spontaneous character voices). A short para that can't merge up starts
        # a new unit and accumulates the following paras instead.
        ceiling = MAX_UNIT_CHARS
        if not theater and prev is not None and _dialogue_heavy(f"{prev['text']} {p}"):
            ceiling = DIALOGUE_MAX_UNIT_CHARS
        if wants_merge and len(prev["text"]) + len(p) + 1 <= ceiling:
            prev["text"] = f"{prev['text']} {p}"
            prev["paras"].append(i)
        else:
            units.append({"text": p, "paras": [i], "pause": False})
    for n, u in enumerate(units):
        u["unit"] = n
    return units


def _book_render_mode(book_id: str) -> tuple[bool, str]:
    """(theater, dials_key) for a book — one lookup, used by every render path."""
    con = _db()
    row = con.execute("SELECT theater FROM books WHERE id=?", (book_id,)).fetchone()
    con.close()
    theater = bool(row["theater"]) if row else False
    return theater, _dials_key_for(theater)


def _cache_path(book_id: str, voice: str, unit_text: str,
                dials_key: str | None = None) -> Path:
    key = dials_key or CONSISTENCY_DIALS_KEY
    h = hashlib.sha256(f"{unit_text}|{voice}|{key}".encode("utf-8")).hexdigest()[:32]
    d = BOOKS_ROOT / "cache" / book_id / voice
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{h}.wav"


def _update_audio_seconds(book_id: str, chapter: int) -> None:
    """Recompute duration-so-far for a chapter from its cached unit files."""
    text = _chapter_path(book_id, chapter).read_text(encoding="utf-8")
    theater, dials_key = _book_render_mode(book_id)
    con = _db()
    voice = (con.execute("SELECT voice FROM books WHERE id=?", (book_id,)).fetchone()
             or {"voice": ""})["voice"] or "clone"
    total = 0.0
    for u in render_units(text, theater=theater):
        if u["pause"]:
            continue
        p = _cache_path(book_id, voice, u["text"], dials_key)
        if p.exists():
            total += max(0, p.stat().st_size - 44) / 2 / 24000
    with con:
        con.execute("UPDATE chapters SET audio_seconds=? WHERE book_id=? AND idx=?",
                    (total, book_id, chapter))
    con.close()


def _render_unit(book_id: str, chapter: int, unit: int, voice: str,
                 force: bool = False) -> Path:
    text = _chapter_path(book_id, chapter)
    if not text.exists():
        raise HTTPException(status_code=404, detail="Chapter not found")
    theater, dials_key = _book_render_mode(book_id)
    units = render_units(text.read_text(encoding="utf-8"), theater=theater)
    if not (0 <= unit < len(units)):
        raise HTTPException(status_code=404, detail="Unit not found")
    u = units[unit]
    if u["pause"]:
        raise HTTPException(status_code=422, detail="Pause unit has no audio")
    path = _cache_path(book_id, voice, u["text"], dials_key)
    if path.exists() and not force:
        return path
    # B3: verified render — the reader service transcribes each take and
    # retakes on transcript mismatch. V2 (consistency mode, default): renders
    # run COOLER (theater lives in the sampling heat) with the conservative
    # speaker-similarity gate alongside — wrong words AND wrong voice both
    # trigger retakes. Theater mode keeps the legacy dials, no similarity.
    import base64
    body: dict = {"text": u["text"], "voice": voice, "verify": True}
    if not theater:
        body["temperature"] = BOOK_TEMPERATURE
        body["similarity"] = True
    try:
        r = httpx.post(f"{READER_BASE}/render", json=body, timeout=600)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Render unavailable ({e}) — retry shortly")
    path.write_bytes(base64.b64decode(data["audio_b64"]))
    con = _db()
    with con:
        con.execute(
            "INSERT OR REPLACE INTO unit_status VALUES (?,?,?,?,?,?,?,?)",
            (book_id, path.stem, chapter, unit, data.get("ratio"),
             data.get("attempts", 1), 1 if data.get("passed") else 0, time.time()))
    con.close()
    _update_audio_seconds(book_id, chapter)
    return path


# ── Pre-render queue (B3): renders ahead at idle, yields to live use ─────────
import threading

_worker_lock = threading.Lock()
_worker_started = False
_progress: dict[str, dict] = {}  # book_id -> {chapter, done, total}


def _should_yield() -> bool:
    """Voice session live, or the user active in the last ~90s (any channel)."""
    if VOICE_FLAG.exists():
        return True
    try:
        return (time.time() - float(LAST_ACTIVE.read_text().strip())) < 90
    except (OSError, ValueError):
        return False


def _prerender_worker():
    logger.info("books: pre-render worker up")
    while True:
        con = _db()
        row = con.execute(
            "SELECT book_id, chapter FROM prerender WHERE status='queued' "
            "ORDER BY queued_at LIMIT 1").fetchone()
        con.close()
        if not row:
            time.sleep(10)
            continue
        book_id, chapter = row["book_id"], row["chapter"]
        con = _db()
        with con:
            con.execute("UPDATE prerender SET status='rendering', started_at=? "
                        "WHERE book_id=? AND chapter=?", (time.time(), book_id, chapter))
        voice = (con.execute("SELECT voice FROM books WHERE id=?", (book_id,)).fetchone()
                 or {"voice": ""})["voice"] or "clone"
        con.close()
        try:
            text_path = _chapter_path(book_id, chapter)
            if not text_path.exists():
                raise FileNotFoundError(chapter)
            theater, dials_key = _book_render_mode(book_id)
            units = [u for u in render_units(text_path.read_text(encoding="utf-8"),
                                             theater=theater)
                     if not u["pause"]]
            t0 = time.time()
            for n, u in enumerate(units):
                while _should_yield():
                    time.sleep(15)  # idle-yield: voice session / active chatting
                _progress[book_id] = {"chapter": chapter, "done": n, "total": len(units)}
                if _cache_path(book_id, voice, u["text"], dials_key).exists():
                    continue
                try:
                    _render_unit(book_id, chapter, u["unit"], voice)
                except HTTPException:
                    time.sleep(20)  # preempted by a live read — back off, retry
                    _render_unit(book_id, chapter, u["unit"], voice)
            con = _db()
            with con:
                con.execute("UPDATE prerender SET status='done', done_at=? "
                            "WHERE book_id=? AND chapter=?", (time.time(), book_id, chapter))
            con.close()
            logger.info("books: pre-rendered %s ch.%d (%d units) in %.0fs",
                        book_id, chapter, len(units), time.time() - t0)
        except Exception as e:
            logger.warning("books: pre-render %s ch.%d failed: %s", book_id, chapter, e)
            con = _db()
            with con:
                con.execute("UPDATE prerender SET status='error', done_at=? "
                            "WHERE book_id=? AND chapter=?", (time.time(), book_id, chapter))
            con.close()
        finally:
            _progress.pop(book_id, None)


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_prerender_worker, daemon=True).start()
            _worker_started = True


# ── Routes ────────────────────────────────────────────────────────────────────

class RenameRequest(BaseModel):
    title: str


class SplitRequest(BaseModel):
    paragraph_index: int  # split BEFORE this paragraph (of the prepared text)


class BookPatch(BaseModel):
    status: str | None = None
    title: str | None = None
    author: str | None = None
    voice: str | None = None


class PositionReq(BaseModel):
    chapter: int
    unit: int


class BookmarkReq(BaseModel):
    chapter: int
    unit: int
    name: str = ""


class HighlightReq(BaseModel):
    chapter: int
    unit: int
    text: str


def register_books_routes(router: APIRouter) -> None:
    # Static paths FIRST — they must beat /books/{book_id} in match order.
    @router.get("/books/voices")
    def list_voices():
        # Seeded with the canonical voice; B5 replaces this with the voice library.
        return {"voices": [{"id": "clone", "label": "Cloned voice"}]}

    @router.get("/books/voice-active")
    def voice_active():
        """Never-overlap: true while a realtime voice session is connected
        (flag file written by voice_bot on client connect/disconnect)."""
        return {"active": VOICE_FLAG.exists()}

    @router.get("/books")
    def list_books():
        con = _db()
        books = [dict(r) for r in con.execute(
            "SELECT * FROM books ORDER BY created_at DESC").fetchall()]
        for b in books:
            row = con.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(audio_seconds), 0) AS s "
                "FROM chapters WHERE book_id = ?", (b["id"],)).fetchone()
            b["chapter_count"] = row["n"]
            b["audio_seconds"] = row["s"]
        con.close()
        return {"books": books}

    @router.post("/books/import")
    async def import_route(file: UploadFile = File(...)):
        raw = await file.read()
        if len(raw) > 40 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large (40MB max)")
        return import_book(file.filename or "book.txt", raw, BOOKS_ROOT)

    @router.get("/books/{book_id}")
    def get_book(book_id: str):
        con = _db()
        b = con.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not b:
            con.close()
            raise HTTPException(status_code=404, detail="Book not found")
        chs = [dict(r) for r in con.execute(
            "SELECT idx, title, char_count, para_count, audio_seconds "
            "FROM chapters WHERE book_id = ? ORDER BY idx", (book_id,)).fetchall()]
        con.close()
        return {**dict(b), "chapters": chs}

    @router.get("/books/{book_id}/chapters/{idx}")
    def get_chapter(book_id: str, idx: int, view: str = "prepared"):
        p = _chapter_path(book_id, idx)
        if not p.exists():
            raise HTTPException(status_code=404, detail="Chapter not found")
        if view == "original":
            src = _book_dir(book_id) / "source.txt"
            return {"text": src.read_text(encoding="utf-8") if src.exists() else ""}
        text = p.read_text(encoding="utf-8")
        theater, dials_key = _book_render_mode(book_id)
        units = render_units(text, theater=theater)
        # Annotate verify state: cached? flagged (failed verification -> shows
        # ⚠ in the follow-along pane for the user's manual re-roll)?
        con = _db()
        voice = (con.execute("SELECT voice FROM books WHERE id=?", (book_id,)).fetchone()
                 or {"voice": ""})["voice"] or "clone"
        status = {r["hash"]: r for r in con.execute(
            "SELECT hash, passed FROM unit_status WHERE book_id=?", (book_id,))}
        con.close()
        for u in units:
            if u["pause"]:
                continue
            h = _cache_path(book_id, voice, u["text"], dials_key)
            u["cached"] = h.exists()
            st = status.get(h.stem)
            u["flagged"] = bool(st and not st["passed"])
        return {
            "text": text,
            "paragraphs": [q for q in text.split("\n\n") if q.strip()],
            "units": units,
        }

    @router.post("/books/{book_id}/theater")
    def set_theater(book_id: str, on: bool = False):
        """V2: per-book character-voice theater toggle. Default OFF = voice
        consistency mode (consistent the agent: cooler temp, tighter dialogue
        units, similarity gate). ON = the legacy spontaneous-casting behavior.
        Flipping changes the dials key, so the other mode's cache is simply a
        different keyspace — no audio is destroyed, re-renders happen lazily."""
        con = _db()
        with con:
            con.execute("UPDATE books SET theater=? WHERE id=?", (1 if on else 0, book_id))
        con.close()
        return {"ok": True, "theater": on}

    @router.post("/books/{book_id}/chapters/{idx}/rename")
    def rename_chapter(book_id: str, idx: int, req: RenameRequest):
        con = _db()
        with con:
            con.execute("UPDATE chapters SET title = ? WHERE book_id = ? AND idx = ?",
                        (req.title.strip(), book_id, idx))
        con.close()
        return {"ok": True}

    @router.post("/books/{book_id}/chapters/{idx}/merge_up")
    def merge_up(book_id: str, idx: int):
        """Merge chapter idx into the previous chapter."""
        if idx <= 0:
            raise HTTPException(status_code=422, detail="First chapter has nothing above it")
        chs = _read_chapters(book_id)
        if idx >= len(chs):
            raise HTTPException(status_code=404, detail="Chapter not found")
        chs[idx - 1]["text"] = chs[idx - 1]["text"].rstrip() + "\n\n" + chs[idx]["text"].lstrip()
        del chs[idx]
        _write_chapters(book_id, chs)
        return {"ok": True, "chapters": len(chs)}

    @router.post("/books/{book_id}/chapters/{idx}/split")
    def split_chapter(book_id: str, idx: int, req: SplitRequest):
        """Split BEFORE the given paragraph index of the prepared text."""
        chs = _read_chapters(book_id)
        if idx >= len(chs):
            raise HTTPException(status_code=404, detail="Chapter not found")
        paras = [q for q in chs[idx]["text"].split("\n\n") if q.strip()]
        if not (0 < req.paragraph_index < len(paras)):
            raise HTTPException(status_code=422, detail="Split point out of range")
        first = "\n\n".join(paras[: req.paragraph_index])
        second = "\n\n".join(paras[req.paragraph_index:])
        new = {"title": chs[idx]["title"] + " (contd.)", "text": second}
        chs[idx]["text"] = first
        chs.insert(idx + 1, new)
        _write_chapters(book_id, chs)
        return {"ok": True, "chapters": len(chs)}

    @router.patch("/books/{book_id}")
    def patch_book(book_id: str, req: BookPatch):
        sets, vals = [], []
        for field in ("status", "title", "author"):
            v = getattr(req, field)
            if v is not None:
                sets.append(f"{field} = ?")
                vals.append(v)
        if not sets:
            return {"ok": True}
        con = _db()
        with con:
            con.execute(f"UPDATE books SET {', '.join(sets)} WHERE id = ?", (*vals, book_id))
        con.close()
        return {"ok": True}

    # ── B2: audio + player state ─────────────────────────────────────────────

    @router.post("/books/{book_id}/prerender")
    def queue_prerender(book_id: str, chapter: int | None = None):
        """Queue one chapter (?chapter=N) or the whole book (overnight mode)."""
        con = _db()
        n = con.execute("SELECT COUNT(*) AS n FROM chapters WHERE book_id=?",
                        (book_id,)).fetchone()["n"]
        if not n:
            con.close()
            raise HTTPException(status_code=404, detail="Book not found")
        targets = [chapter] if chapter is not None else list(range(n))
        with con:
            for c in targets:
                con.execute(
                    "INSERT INTO prerender (book_id, chapter, status, queued_at) "
                    "VALUES (?,?, 'queued', ?) ON CONFLICT(book_id, chapter) DO UPDATE "
                    "SET status='queued', queued_at=excluded.queued_at "
                    "WHERE prerender.status IN ('error', 'done')",
                    (book_id, c, time.time()))
        con.close()
        _ensure_worker()
        return {"queued": len(targets)}

    @router.get("/books/{book_id}/prerender")
    def prerender_status(book_id: str):
        con = _db()
        rows = [dict(r) for r in con.execute(
            "SELECT chapter, status FROM prerender WHERE book_id=? ORDER BY chapter",
            (book_id,))]
        con.close()
        done = sum(1 for r in rows if r["status"] == "done")
        return {"chapters": rows, "done": done, "total": len(rows),
                "current": _progress.get(book_id)}

    @router.get("/books/{book_id}/render-stats")
    def render_stats(book_id: str):
        """Verify pass / retake / fail rates (the user's B3 report requirement)."""
        con = _db()
        r = con.execute(
            "SELECT COUNT(*) AS units, SUM(passed) AS passed, "
            "SUM(CASE WHEN attempts > 1 THEN 1 ELSE 0 END) AS retaken, "
            "SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) AS failed, "
            "AVG(ratio) AS avg_ratio, SUM(attempts) AS total_attempts "
            "FROM unit_status WHERE book_id=?", (book_id,)).fetchone()
        con.close()
        return dict(r)

    @router.get("/books/{book_id}/audio/{chapter}/{unit}")
    def unit_audio(book_id: str, chapter: int, unit: int, voice: str = "clone"):
        path = _render_unit(book_id, chapter, unit, voice)
        return FileResponse(path, media_type="audio/wav")

    @router.post("/books/{book_id}/audio/{chapter}/{unit}/reroll")
    def unit_reroll(book_id: str, chapter: int, unit: int, voice: str = "clone"):
        _render_unit(book_id, chapter, unit, voice, force=True)
        return {"ok": True}

    @router.get("/books/{book_id}/position")
    def get_position(book_id: str):
        con = _db()
        r = con.execute("SELECT * FROM positions WHERE book_id=?", (book_id,)).fetchone()
        con.close()
        return dict(r) if r else {"book_id": book_id, "chapter": 0, "unit": 0, "updated_at": None}

    @router.put("/books/{book_id}/position")
    def put_position(book_id: str, req: PositionReq):
        con = _db()
        with con:
            con.execute(
                "INSERT INTO positions (book_id, chapter, unit, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(book_id) DO UPDATE SET chapter=excluded.chapter, "
                "unit=excluded.unit, updated_at=excluded.updated_at",
                (book_id, req.chapter, req.unit, time.time()))
        con.close()
        return {"ok": True}

    @router.get("/books/{book_id}/bookmarks")
    def list_bookmarks(book_id: str):
        con = _db()
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM bookmarks WHERE book_id=? ORDER BY created_at DESC", (book_id,))]
        con.close()
        return {"bookmarks": rows}

    @router.post("/books/{book_id}/bookmarks")
    def add_bookmark(book_id: str, req: BookmarkReq):
        con = _db()
        with con:
            con.execute("INSERT INTO bookmarks VALUES (?,?,?,?,?,?)",
                        (uuid.uuid4().hex[:10], book_id, req.chapter, req.unit,
                         req.name.strip(), time.time()))
        con.close()
        return {"ok": True}

    @router.delete("/books/{book_id}/bookmarks/{bm_id}")
    def del_bookmark(book_id: str, bm_id: str):
        con = _db()
        with con:
            con.execute("DELETE FROM bookmarks WHERE id=? AND book_id=?", (bm_id, book_id))
        con.close()
        return {"ok": True}

    @router.get("/books/{book_id}/highlights")
    def list_highlights(book_id: str):
        con = _db()
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM highlights WHERE book_id=? ORDER BY created_at DESC", (book_id,))]
        con.close()
        return {"highlights": rows}

    @router.post("/books/{book_id}/highlights")
    def add_highlight(book_id: str, req: HighlightReq):
        con = _db()
        with con:
            con.execute("INSERT INTO highlights VALUES (?,?,?,?,?,?)",
                        (uuid.uuid4().hex[:10], book_id, req.chapter, req.unit,
                         req.text.strip()[:2000], time.time()))
        con.close()
        return {"ok": True}

    @router.delete("/books/{book_id}/highlights/{h_id}")
    def del_highlight(book_id: str, h_id: str):
        con = _db()
        with con:
            con.execute("DELETE FROM highlights WHERE id=? AND book_id=?", (h_id, book_id))
        con.close()
        return {"ok": True}

    @router.delete("/books/{book_id}")
    def delete_book(book_id: str):
        import shutil
        con = _db()
        with con:
            con.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))
            con.execute("DELETE FROM books WHERE id = ?", (book_id,))
        con.close()
        bdir = _book_dir(book_id)
        if bdir.exists():
            shutil.rmtree(bdir, ignore_errors=True)
        cache = BOOKS_ROOT / "cache" / book_id
        if cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
        return {"ok": True}
