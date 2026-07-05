"""
knowledge_db.py — Database query functions for the Knowledge RAG system.

Public API used by knowledge_tools.py:
  search(query, mod_group, doc_type, include_community, limit, db_url) -> list[dict]
  get_document(doc_id, db_url) -> dict | None
  list_documents(mod_group, db_url) -> list[dict]
  add_correction(mod_group, topic, correction_text, source_message_id,
                 source_channel_id, source_author_id, source_author_name, db_url) -> int
  classify_mod_group(query, db_url) -> str

Retrieval pipeline (per search call):
  1. Classify mod_group from disambiguation_rules (keyword match, no LLM cost)
  2. Parallel semantic search (vector cosine) + keyword search (tsvector)
  3. Merge + deduplicate, combined score = (semantic*0.7) + (keyword*0.3)
  4. Tier 1 wins over Tier 2 when scores within 0.05
  5. Return top `limit` results with metadata
"""
from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=True)

logger = logging.getLogger(__name__)

# Tier conflict tolerance — Tier 1 wins if score within this margin
TIER_CONFLICT_MARGIN = 0.05

# Words common in questions but rarely in knowledge docs — strip for keyword search.
# plainto_tsquery ANDs all terms; conversational fluff causes zero matches.
_KEYWORD_STOP = frozenset({
    "i", "me", "my", "was", "is", "are", "the", "a", "an", "from", "to", "for",
    "wondering", "wonder", "whether", "rather", "actually", "just", "really",
    "maybe", "perhaps", "think", "wanted", "want", "know", "asking", "asked",
    "question", "questions", "curious", "wondered", "basically", "simply",
    "please", "thanks", "thank", "got", "get", "like", "need", "does", "did",
    "would", "could", "should", "will", "were", "and", "or", "but", "if", "with",
    "than", "not", "that", "this", "what", "how", "when", "where", "which", "who",
    "whom", "why", "can", "it", "its", "itself",
})


def _extract_keyword_terms(query: str, max_terms: int = 10) -> str:
    """
    Extract substantive terms for keyword search. plainto_tsquery ANDs all words;
    conversational phrases like 'I was wondering' cause zero matches because
    'wondering' is not in the knowledge docs.
    """
    words = re.sub(r"[^\w\s]", " ", query.lower()).split()
    kept = [w for w in words if len(w) >= 2 and w not in _KEYWORD_STOP]
    return " ".join(kept[:max_terms]) if kept else query


def _get_connection(db_url: str):
    import psycopg
    from pgvector.psycopg import register_vector
    conn = psycopg.connect(db_url)
    register_vector(conn)
    return conn


def _get_public_url() -> str:
    return os.environ.get("KNOWLEDGE_DATABASE_URL", "").strip()


def _get_admin_url() -> str:
    return os.environ.get("ADMIN_KNOWLEDGE_DATABASE_URL", "").strip()


# ── Disambiguation rules cache ────────────────────────────────────────────────

_rules_cache: list[dict] | None = None
_rules_cache_db: str | None = None


def _load_disambiguation_rules(db_url: str) -> list[dict]:
    """Load disambiguation rules from DB, cached per process."""
    global _rules_cache, _rules_cache_db
    if _rules_cache is not None and _rules_cache_db == db_url:
        return _rules_cache
    try:
        conn = _get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT keyword, mod_group, doc_type, priority
                FROM knowledge_disambiguation_rules
                WHERE is_active = TRUE
                ORDER BY priority DESC
                """
            )
            rows = cur.fetchall()
        conn.close()
        _rules_cache = [
            {"keyword": r[0], "mod_group": r[1], "doc_type": r[2], "priority": r[3]}
            for r in rows
        ]
        _rules_cache_db = db_url
    except Exception as e:
        logger.warning("Could not load disambiguation rules: %s", e)
        _rules_cache = []
    return _rules_cache or []


def invalidate_rules_cache() -> None:
    """Call after updating disambiguation rules."""
    global _rules_cache
    _rules_cache = None


def classify_mod_group(query: str, db_url: str | None = None) -> tuple[str, str | None]:
    """
    Classify a query into (mod_group, doc_type) using disambiguation rules.

    Returns ('general', None) if no match found.
    Rules are checked by priority (highest first).
    """
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return "general", None

    q_lower = query.lower()
    rules = _load_disambiguation_rules(db_url)

    for rule in rules:
        keyword = rule["keyword"].lower()
        if keyword in q_lower:
            return rule["mod_group"], rule.get("doc_type")

    return "general", None


# ── Semantic search ───────────────────────────────────────────────────────────

def _search_semantic(
    query_vec: list[float],
    mod_group: str,
    doc_type: str | None,
    limit: int,
    conn,
    access_level: str = "public",
    project: str | list[str] | None = None,
) -> list[dict]:
    """Vector cosine similarity search over knowledge_chunks."""
    try:
        conditions = ["kc.is_embedded = TRUE", "kd.is_active = TRUE"]
        params: list = [query_vec]

        # Privacy filter: public channels exclude private documents
        if access_level == "public":
            conditions.append("kd.is_private = FALSE")

        if mod_group and mod_group != "general":
            conditions.append("kc.mod_group = %s")
            params.append(mod_group)
        if doc_type:
            conditions.append("kc.doc_type = %s")
            params.append(doc_type)
        if project:
            if isinstance(project, (list, tuple)):
                conditions.append("kd.project = ANY(%s)")
                params.append(list(project))
            else:
                conditions.append("kd.project = %s")
                params.append(project)

        where = " AND ".join(conditions)
        params.append(query_vec)  # second %s::vector for ORDER BY
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT kc.chunk_id, kc.doc_id, kc.mod_group, kc.doc_type, kc.tier,
                       kc.section_header, kc.content, kc.token_count,
                       1 - (kc.embedding <=> %s::vector) AS similarity,
                       kd.title
                FROM knowledge_chunks kc
                JOIN knowledge_documents kd ON kd.doc_id = kc.doc_id
                WHERE {where}
                ORDER BY kc.embedding <=> %s::vector
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

        return [
            {
                "chunk_id": r[0],
                "doc_id": r[1],
                "mod_group": r[2],
                "doc_type": r[3],
                "tier": r[4],
                "section_header": r[5],
                "content": r[6],
                "token_count": r[7],
                "semantic_score": float(r[8]),
                "keyword_score": 0.0,
                "doc_title": r[9],
                "source": "chunk",
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Semantic search failed: %s", e)
        return []


def _search_keyword(
    query: str,
    mod_group: str,
    doc_type: str | None,
    limit: int,
    conn,
    access_level: str = "public",
    project: str | list[str] | None = None,
) -> list[dict]:
    """tsvector full-text search over knowledge_chunks."""
    try:
        kw_query = _extract_keyword_terms(query)
        conditions = ["kc.content_tsv @@ plainto_tsquery('english', %s)", "kd.is_active = TRUE"]
        params: list[object] = [kw_query]

        # Privacy filter: public channels exclude private documents
        if access_level == "public":
            conditions.append("kd.is_private = FALSE")

        if mod_group and mod_group != "general":
            conditions.append("kc.mod_group = %s")
            params.append(mod_group)
        if doc_type:
            conditions.append("kc.doc_type = %s")
            params.append(doc_type)
        if project:
            if isinstance(project, (list, tuple)):
                conditions.append("kd.project = ANY(%s)")
                params.append(list(project))
            else:
                conditions.append("kd.project = %s")
                params.append(project)

        where = " AND ".join(conditions)
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT kc.chunk_id, kc.doc_id, kc.mod_group, kc.doc_type, kc.tier,
                       kc.section_header, kc.content, kc.token_count,
                       ts_rank(kc.content_tsv, plainto_tsquery('english', %s)) AS rank,
                       kd.title
                FROM knowledge_chunks kc
                JOIN knowledge_documents kd ON kd.doc_id = kc.doc_id
                WHERE {where}
                ORDER BY rank DESC
                LIMIT %s
                """,
                [kw_query] + params,
            )
            rows = cur.fetchall()

        return [
            {
                "chunk_id": r[0],
                "doc_id": r[1],
                "mod_group": r[2],
                "doc_type": r[3],
                "tier": r[4],
                "section_header": r[5],
                "content": r[6],
                "token_count": r[7],
                "semantic_score": 0.0,
                "keyword_score": float(r[8]),
                "doc_title": r[9],
                "source": "chunk",
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Keyword search failed: %s", e)
        return []


def _search_corrections_semantic(
    query_vec: list[float],
    mod_group: str,
    limit: int,
    conn,
) -> list[dict]:
    """Semantic search over community_corrections."""
    try:
        conditions = ["cc.is_embedded = TRUE", "cc.status != 'rejected'"]
        params: list = [query_vec]

        if mod_group and mod_group != "general":
            conditions.append("cc.mod_group = %s")
            params.append(mod_group)

        where = " AND ".join(conditions)
        params.append(query_vec)  # second %s::vector for ORDER BY
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT cc.correction_id, cc.mod_group, cc.topic, cc.correction_text,
                       cc.status, 1 - (cc.embedding <=> %s::vector) AS similarity
                FROM community_corrections cc
                WHERE {where}
                ORDER BY cc.embedding <=> %s::vector
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

        return [
            {
                "chunk_id": f"cc_{r[0]}",
                "correction_id": r[0],
                "mod_group": r[1],
                "doc_type": "correction",
                "tier": 2,
                "section_header": r[2],   # topic as section header
                "content": r[3],
                "token_count": 0,
                "semantic_score": float(r[5]),
                "keyword_score": 0.0,
                "doc_title": f"Community Correction — {r[2]}",
                "source": "correction",
                "status": r[4],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Corrections semantic search failed: %s", e)
        return []


def _search_corrections_keyword(
    query: str,
    mod_group: str,
    limit: int,
    conn,
) -> list[dict]:
    """Keyword search over community_corrections."""
    try:
        kw_query = _extract_keyword_terms(query)
        conditions = ["cc.content_tsv @@ plainto_tsquery('english', %s)", "cc.status != 'rejected'"]
        params: list[object] = [kw_query]

        if mod_group and mod_group != "general":
            conditions.append("cc.mod_group = %s")
            params.append(mod_group)

        where = " AND ".join(conditions)
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT cc.correction_id, cc.mod_group, cc.topic, cc.correction_text,
                       cc.status, ts_rank(cc.content_tsv, plainto_tsquery('english', %s)) AS rank
                FROM community_corrections cc
                WHERE {where}
                ORDER BY rank DESC
                LIMIT %s
                """,
                [kw_query] + params,
            )
            rows = cur.fetchall()

        return [
            {
                "chunk_id": f"cc_{r[0]}",
                "correction_id": r[0],
                "mod_group": r[1],
                "doc_type": "correction",
                "tier": 2,
                "section_header": r[2],
                "content": r[3],
                "token_count": 0,
                "semantic_score": 0.0,
                "keyword_score": float(r[5]),
                "doc_title": f"Community Correction — {r[2]}",
                "source": "correction",
                "status": r[4],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Corrections keyword search failed: %s", e)
        return []


# ── Merge and rank ────────────────────────────────────────────────────────────

def _merge_results(semantic: list[dict], keyword: list[dict]) -> list[dict]:
    """
    Merge semantic and keyword results. Deduplicate by chunk_id.
    Combined score = (semantic * 0.7) + (keyword * 0.3).
    Tier 1 wins over Tier 2 when scores within TIER_CONFLICT_MARGIN.
    """
    merged: dict[object, dict] = {}

    for result in semantic:
        cid = result["chunk_id"]
        merged[cid] = result.copy()
        merged[cid]["combined_score"] = result["semantic_score"] * 0.7

    for result in keyword:
        cid = result["chunk_id"]
        if cid in merged:
            merged[cid]["keyword_score"] = result["keyword_score"]
            merged[cid]["combined_score"] = (
                merged[cid]["semantic_score"] * 0.7
                + result["keyword_score"] * 0.3
            )
        else:
            merged[cid] = result.copy()
            merged[cid]["combined_score"] = result["keyword_score"] * 0.3

    # Sort by combined score descending
    ranked = sorted(merged.values(), key=lambda x: x["combined_score"], reverse=True)

    # Apply tier preference: Tier 1 wins over Tier 2 within margin
    result_list = list(ranked)
    for i in range(len(result_list) - 1):
        a = result_list[i]
        b = result_list[i + 1]
        if a["tier"] == 2 and b["tier"] == 1:
            score_diff = abs(a["combined_score"] - b["combined_score"])
            if score_diff <= TIER_CONFLICT_MARGIN:
                result_list[i], result_list[i + 1] = result_list[i + 1], result_list[i]

    return result_list


# ── Main search function ──────────────────────────────────────────────────────

def search(
    query: str,
    mod_group: str | None = None,
    doc_type: str | None = None,
    include_community: bool = True,
    limit: int = 6,
    db_url: str | None = None,
    access_level: str = "public",
    project: str | list[str] | None = None,
) -> list[dict]:
    """
    Dual search: semantic (vector) + keyword (tsvector), merged and ranked.

    Args:
        query: Natural language search query.
        mod_group: 'pf'|'omega'|'server'|'general' or None to auto-classify.
        doc_type: Optional type filter.
        include_community: Include Tier 2 community corrections.
        limit: Max results to return.
        db_url: Database URL (defaults to KNOWLEDGE_DATABASE_URL).
        access_level: 'public' or 'admin'. Public excludes private documents.
        project: Optional knowledge_documents.project filter (e.g. 'rpg-<slug>'),
            or a LIST of projects searched together (e.g. a story project plus
            'rpg-common' shared lore). When given, mod_group auto-classification
            is skipped — the project IS the scope, and a guessed mod_group
            would wrongly exclude project docs stored under 'general'.

    Returns list of result dicts with keys:
        chunk_id, doc_id, mod_group, doc_type, tier, section_header,
        content, token_count, combined_score, doc_title, source, [status]
    """
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        logger.error("KNOWLEDGE_DATABASE_URL not set")
        return []

    # Auto-classify mod_group if not provided (skipped for project-scoped
    # searches — see docstring).
    if not mod_group and not project:
        mod_group, inferred_doc_type = classify_mod_group(query, db_url)
        if not doc_type:
            doc_type = inferred_doc_type

    # Get query embedding (may be None if Ollama down)
    from .embedder import embed
    query_vec = embed(query)

    try:
        conn = _get_connection(db_url)
    except Exception as e:
        logger.error("Cannot connect to knowledge DB: %s", e)
        return []

    try:
        # Run semantic + keyword searches in parallel
        semantic_chunks: list[dict] = []
        keyword_chunks: list[dict] = []
        semantic_corrections: list[dict] = []
        keyword_corrections: list[dict] = []

        search_limit = limit * 2  # fetch more, then trim after merge

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}

            if query_vec is not None:
                futures["sem_chunks"] = executor.submit(
                    _search_semantic, query_vec, mod_group, doc_type, search_limit, conn,
                    access_level, project
                )
                if include_community:
                    futures["sem_corrections"] = executor.submit(
                        _search_corrections_semantic, query_vec, mod_group, search_limit // 2, conn
                    )

            futures["kw_chunks"] = executor.submit(
                _search_keyword, query, mod_group, doc_type, search_limit, conn,
                access_level, project
            )
            if include_community:
                futures["kw_corrections"] = executor.submit(
                    _search_corrections_keyword, query, mod_group, search_limit // 2, conn
                )

            for key, fut in futures.items():
                try:
                    result = fut.result(timeout=10)
                    if key == "sem_chunks":
                        semantic_chunks = result
                    elif key == "kw_chunks":
                        keyword_chunks = result
                    elif key == "sem_corrections":
                        semantic_corrections = result
                    elif key == "kw_corrections":
                        keyword_corrections = result
                except Exception as e:
                    logger.warning("Search future %s failed: %s", key, e)

        # Merge chunks and corrections separately, then combine
        merged_chunks = _merge_results(semantic_chunks, keyword_chunks)
        merged_corrections = _merge_results(semantic_corrections, keyword_corrections)

        # Combine: take top chunks, then append corrections up to limit
        combined = merged_chunks[:limit]
        if include_community:
            slots_left = limit - len(combined)
            if slots_left > 0:
                combined.extend(merged_corrections[:slots_left])

        # Sort final combined list by combined_score, respecting tier
        combined.sort(key=lambda x: x["combined_score"], reverse=True)

        return combined[:limit]

    finally:
        conn.close()


def ensure_chunks_and_embed(doc_id: int, db_url: str | None = None) -> dict:
    """Chunk + embed a document (idempotent): creates chunks from full_text if
    the doc has none, embeds any unembedded chunks (needs Ollama), and keeps
    knowledge_documents.chunk_count honest. Safe when Ollama is down — chunks
    are stored keyword-searchable (is_embedded=false) and embedding can be
    retried later via the same call (the Data tab's ⚡ Embed button).

    Returns {chunks, embedded, pending, created, ollama_ok, error?}."""
    if not db_url:
        db_url = _get_admin_url() or _get_public_url()
    if not db_url:
        return {"error": "KNOWLEDGE_DATABASE_URL not set"}
    conn = _get_connection(db_url)
    created = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT full_text, mod_group, doc_type, tier FROM knowledge_documents "
                        "WHERE doc_id=%s AND is_active=TRUE", (doc_id,))
            row = cur.fetchone()
            if not row:
                return {"error": f"doc {doc_id} not found or inactive"}
            full_text, mod_group, doc_type, tier = row[0], row[1], row[2], row[3]
            cur.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE doc_id=%s", (doc_id,))
            n_chunks = cur.fetchone()[0]

        if n_chunks == 0 and (full_text or "").strip():
            from scripts.rechunk_knowledge import chunk_document
            chunks = chunk_document(full_text)
            with conn.cursor() as cur:
                for c in chunks:
                    cur.execute(
                        "INSERT INTO knowledge_chunks (doc_id, mod_group, doc_type, tier, "
                        "chunk_index, section_header, content, token_count, is_embedded) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE)",
                        (doc_id, mod_group or "general", doc_type or "other", tier or 1,
                         c["chunk_index"], c.get("section_header"), c["content"],
                         c.get("token_count", 0)))
            conn.commit()
            created = len(chunks)

        # Embed whatever is pending (newly created or previously skipped).
        ollama_ok = True
        embedded_now = 0
        with conn.cursor() as cur:
            cur.execute("SELECT chunk_id, content FROM knowledge_chunks "
                        "WHERE doc_id=%s AND NOT is_embedded ORDER BY chunk_index", (doc_id,))
            pending_rows = cur.fetchall()
        if pending_rows:
            from .embedder import embed_batch
            vectors = embed_batch([r[1] for r in pending_rows])
            with conn.cursor() as cur:
                for (chunk_id, _), vec in zip(pending_rows, vectors):
                    if vec is not None:
                        cur.execute("UPDATE knowledge_chunks SET embedding=%s, is_embedded=TRUE "
                                    "WHERE chunk_id=%s", (vec, chunk_id))
                        embedded_now += 1
            conn.commit()
            ollama_ok = embedded_now > 0 or not pending_rows

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), SUM(CASE WHEN is_embedded THEN 1 ELSE 0 END) "
                        "FROM knowledge_chunks WHERE doc_id=%s", (doc_id,))
            total, emb = cur.fetchone()
            cur.execute("UPDATE knowledge_documents SET chunk_count=%s WHERE doc_id=%s",
                        (total, doc_id))
        conn.commit()
        return {"chunks": total, "embedded": int(emb or 0),
                "pending": total - int(emb or 0), "created": created,
                "ollama_ok": (total - int(emb or 0)) == 0 or embedded_now > 0}
    except Exception as e:
        logger.warning("ensure_chunks_and_embed(%s) failed: %s", doc_id, e)
        return {"error": str(e), "created": created}
    finally:
        conn.close()


# ── Document management ───────────────────────────────────────────────────────

def get_document(doc_id: int, db_url: str | None = None) -> dict | None:
    """Get document metadata + full text by doc_id."""
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return None
    try:
        conn = _get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, title, mod_group, doc_type, project, tier, is_active,
                       is_private, version, uploaded_at, uploaded_by, full_text, chunk_count, notes
                FROM knowledge_documents WHERE doc_id = %s
                """,
                (doc_id,),
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "doc_id": row[0], "title": row[1], "mod_group": row[2],
            "doc_type": row[3], "project": row[4], "tier": row[5], "is_active": row[6],
            "is_private": row[7], "version": row[8],
            "uploaded_at": row[9].isoformat() if row[9] else None,
            "uploaded_by": row[10], "full_text": row[11],
            "chunk_count": row[12], "notes": row[13],
        }
    except Exception as e:
        logger.warning("get_document failed: %s", e)
        return None


def list_documents(
    mod_group: str | None = None,
    doc_type: str | None = None,
    project: str | None = None,
    search: str | None = None,
    active_only: bool = True,
    limit: int = 50,
    offset: int = 0,
    db_url: str | None = None,
) -> tuple[list[dict], int]:
    """List documents with optional filters. Returns (documents, total_count)."""
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return [], 0
    try:
        conn = _get_connection(db_url)
        conditions = []
        params: list = []
        if active_only:
            conditions.append("is_active = TRUE")
        if mod_group:
            conditions.append("mod_group = %s")
            params.append(mod_group)
        if doc_type:
            conditions.append("doc_type = %s")
            params.append(doc_type)
        if project:
            conditions.append("project = %s")
            params.append(project)
        if search:
            conditions.append("(title ILIKE %s OR full_text ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        
        with conn.cursor() as cur:
            # Get total count
            cur.execute(f"SELECT COUNT(*) FROM knowledge_documents {where}", params)
            total = cur.fetchone()[0]
            
            # Get paginated results
            cur.execute(
                f"""
                SELECT doc_id, title, mod_group, doc_type, project, tier, is_active,
                       is_private, chunk_count, uploaded_at, uploaded_by, notes
                FROM knowledge_documents
                {where}
                ORDER BY uploaded_at DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            rows = cur.fetchall()
        conn.close()
        return [
            {
                "doc_id": r[0], "title": r[1], "mod_group": r[2],
                "doc_type": r[3], "project": r[4], "tier": r[5], "is_active": r[6],
                "is_private": r[7], "chunk_count": r[8],
                "uploaded_at": r[9].isoformat() if r[9] else None,
                "uploaded_by": r[10], "notes": r[11],
            }
            for r in rows
        ], total
    except Exception as e:
        logger.warning("list_documents failed: %s", e)
        return [], 0


def _embed_correction_immediately(correction_id: int, topic: str, correction_text: str, db_url: str) -> None:
    """
    Embed a correction in a background thread so semantic (pgvector) search finds it.
    Fire-and-forget — never blocks add_correction. Same format as embed_knowledge.py.
    """
    def _run() -> None:
        try:
            from .embedder import embed
            text = f"{topic}\n\n{correction_text}" if topic else correction_text
            vec = embed(text)
            if vec is None:
                return
            conn = _get_connection(db_url)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE community_corrections SET embedding = %s, is_embedded = TRUE WHERE correction_id = %s",
                        (vec, correction_id),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.debug("Immediate embed for correction %s failed: %s (embed job will pick up)", correction_id, e)

    threading.Thread(target=_run, daemon=True).start()


def add_correction(
    mod_group: str,
    topic: str,
    correction_text: str,
    source_message_id: str | None = None,
    source_channel_id: str | None = None,
    source_author_id: str | None = None,
    source_author_name: str | None = None,
    source_message_at=None,
    auto_approve: bool = False,
    db_url: str | None = None,
) -> int | None:
    """
    Insert a community correction. Returns correction_id on success, None on failure.
    Triggers immediate embedding (fire-and-forget) so semantic search finds it within seconds.
    auto_approve=True skips the review queue (use for verified admin corrections).
    source_message_at: original Discord message timestamp (optional).
    """
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return None
    status = "approved" if auto_approve else "pending"
    try:
        conn = _get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO community_corrections
                    (mod_group, topic, correction_text,
                     source_message_id, source_channel_id,
                     source_author_id, source_author_name,
                     source_message_at, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING correction_id
                """,
                (
                    mod_group, topic, correction_text,
                    source_message_id, source_channel_id,
                    source_author_id, source_author_name,
                    source_message_at, status,
                ),
            )
            correction_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        # Embed immediately so pgvector semantic search finds it (fire-and-forget)
        _embed_correction_immediately(correction_id, topic, correction_text, db_url)
        return correction_id
    except Exception as e:
        logger.warning("add_correction failed: %s", e)
        return None


def update_document_full_text(
    doc_id: int,
    full_text: str,
    source_filename: str | None = None,
    db_url: str | None = None,
) -> bool:
    """Replace full_text of an existing document. Call rechunk + embed after."""
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return False
    try:
        conn = _get_connection(db_url)
        with conn.cursor() as cur:
            if source_filename:
                cur.execute(
                    "UPDATE knowledge_documents SET full_text = %s, source_filename = %s WHERE doc_id = %s",
                    (full_text, source_filename, doc_id),
                )
            else:
                cur.execute(
                    "UPDATE knowledge_documents SET full_text = %s WHERE doc_id = %s",
                    (full_text, doc_id),
                )
            updated = cur.rowcount
            if updated:
                cur.execute(
                    "INSERT INTO knowledge_upload_log (doc_id, action, performed_by, notes) VALUES (%s, 'replace', 'admin', %s)",
                    (doc_id, source_filename or "replace"),
                )
        conn.commit()
        conn.close()
        return updated > 0
    except Exception as e:
        logger.warning("update_document_full_text failed: %s", e)
        return False


def upload_document(
    title: str,
    mod_group: str,
    doc_type: str,
    full_text: str,
    source_filename: str | None = None,
    uploaded_by: str = "admin",
    notes: str | None = None,
    is_private: bool = False,
    project: str | None = None,
    db_url: str | None = None,
) -> int | None:
    """
    Insert a new document record. Returns doc_id.
    Does NOT chunk or embed — call rechunk_knowledge.py separately.
    
    Args:
        is_private: If True, document is only searchable in admin/DM contexts.
        project: Custom project name (e.g., "the agent", "CG-Discord-Bot")
    """
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return None
    try:
        conn = _get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO knowledge_documents
                    (title, mod_group, doc_type, project, source_filename, full_text, uploaded_by, notes, is_private)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING doc_id
                """,
                (title, mod_group, doc_type, project, source_filename, full_text, uploaded_by, notes, is_private),
            )
            doc_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO knowledge_upload_log (doc_id, action, performed_by) VALUES (%s, 'upload', %s)",
                (doc_id, uploaded_by),
            )
        conn.commit()
        conn.close()
        return doc_id
    except Exception as e:
        logger.warning("upload_document failed: %s", e)
        return None


def list_projects(db_url: str | None = None) -> list[str]:
    """Return a list of unique project names (for dropdown population)."""
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return []
    try:
        conn = _get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT project 
                FROM knowledge_documents 
                WHERE project IS NOT NULL AND project != ''
                ORDER BY project
                """
            )
            projects = [row[0] for row in cur.fetchall()]
        conn.close()
        return projects
    except Exception as e:
        logger.warning("list_projects failed: %s", e)
        return []


def get_corrections_pending(limit: int = 50, db_url: str | None = None) -> list[dict]:
    """Return pending community corrections for admin review."""
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return []
    try:
        conn = _get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT correction_id, mod_group, topic, correction_text,
                       source_message_id, source_channel_id,
                       source_author_name, extracted_at, status
                FROM community_corrections
                WHERE status = 'pending'
                ORDER BY extracted_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        conn.close()
        return [
            {
                "correction_id": r[0], "mod_group": r[1], "topic": r[2],
                "correction_text": r[3], "source_message_id": r[4],
                "source_channel_id": r[5], "source_author_name": r[6],
                "extracted_at": r[7].isoformat() if r[7] else None,
                "status": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("get_corrections_pending failed: %s", e)
        return []


def update_correction_status(
    correction_id: int,
    status: str,
    reviewed_by: str = "admin",
    admin_notes: str | None = None,
    db_url: str | None = None,
) -> bool:
    """Update correction status (approved/rejected/superseded)."""
    if not db_url:
        db_url = _get_public_url()
    if not db_url:
        return False
    try:
        conn = _get_connection(db_url)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE community_corrections
                SET status = %s, reviewed_by = %s, reviewed_at = NOW(), admin_notes = %s
                WHERE correction_id = %s
                """,
                (status, reviewed_by, admin_notes, correction_id),
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning("update_correction_status failed: %s", e)
        return False


def update_document(
    doc_id: int,
    title: str | None = None,
    mod_group: str | None = None,
    doc_type: str | None = None,
    project: str | None = None,
    full_text: str | None = None,
    is_private: bool | None = None,
    is_active: bool | None = None,
    db_url: str | None = None,
) -> bool:
    """Update a document's fields. Returns True if successful."""
    db_url = db_url or _get_admin_url() or _get_public_url()
    if not db_url:
        return False
    try:
        conn = _get_connection(db_url)
        cur = conn.cursor()
        
        # Build dynamic update
        fields = []
        values = []
        if title is not None:
            fields.append("title = %s")
            values.append(title)
        if mod_group is not None:
            fields.append("mod_group = %s")
            values.append(mod_group)
        if doc_type is not None:
            fields.append("doc_type = %s")
            values.append(doc_type)
        if project is not None:
            fields.append("project = %s")
            values.append(project)
        if full_text is not None:
            fields.append("full_text = %s")
            values.append(full_text)
        if is_private is not None:
            fields.append("is_private = %s")
            values.append(is_private)
        if is_active is not None:
            fields.append("is_active = %s")
            values.append(is_active)
            
        if not fields:
            conn.close()
            return True  # Nothing to update
            
        values.append(doc_id)
        cur.execute(
            f"UPDATE knowledge_documents SET {', '.join(fields)} WHERE doc_id = %s",
            values,
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning("update_document failed: %s", e)
        return False


def delete_document(doc_id: int, db_url: str | None = None) -> bool:
    """Delete a document and all its chunks."""
    db_url = db_url or _get_admin_url() or _get_public_url()
    if not db_url:
        return False
    try:
        conn = _get_connection(db_url)
        cur = conn.cursor()
        # Delete chunks first (foreign key will handle this, but explicit is safer)
        cur.execute("DELETE FROM knowledge_chunks WHERE doc_id = %s", (doc_id,))
        # Delete document
        cur.execute("DELETE FROM knowledge_documents WHERE doc_id = %s", (doc_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning("delete_document failed: %s", e)
        return False


def is_configured(admin: bool = False) -> bool:
    """Check if the relevant knowledge DB URL is set."""
    if admin:
        return bool(_get_admin_url())
    return bool(_get_public_url())
