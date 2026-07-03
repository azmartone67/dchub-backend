"""
brain_rag.py — general context-assembly RAG (pgvector + Cohere embeddings).

Started as brain-corpus recall for the L6 planner; now a CORPUS-REGISTRY RAG:
"roll RAG out to a new corpus" = add a row to CORPORA (no new code), same as the
grid master shell's dataset registry. Numbers stay in SQL — this embeds only
UNSTRUCTURED prose (findings/recs/news/deals). Owner-greenlit 2026-07-03.

Store:  brain_corpus_embeddings(source_table, source_id, kind, text,
        embedding vector(1024))  — pgvector on Neon, one table for all corpora.
Embed:  Cohere embed-english-v3.0 (1024-d, asymmetric search_document/search_query;
        OpenAI key is out of quota). Batched ≤96/call.
Recall: cosine (<=>), optionally scoped to one corpus.

Endpoints (admin, X-Admin-Key):
  POST /api/v1/admin/brain/rag/reindex?cap=500    — embed up to cap new rows (any corpus)
  GET  /api/v1/admin/brain/rag/retrieve?q=&k=8&corpus=news_articles — test
  GET  /api/v1/admin/brain/rag/status             — per-corpus coverage
Kill: BRAIN_RAG_DISABLED=1 (endpoints) / BRAIN_RAG_ENABLED unset (planner wiring).
Kept fresh by cron_heartbeat (brain_rag_reindex).
"""
import os
import json
import hmac
import urllib.request
import urllib.error

from flask import Blueprint, jsonify, request

brain_rag_bp = Blueprint("brain_rag", __name__)

EMBED_MODEL = "embed-english-v3.0"
EMBED_DIM = 1024
_COHERE_BATCH = 96  # Cohere v1/embed hard limit

# ── corpus registry — add a row to roll RAG onto a new source (no new code) ──
# Each: source_table → {id (t-qualified ::text), text (SQL expr over alias t),
# kind, where}. All exprs are hardcoded/trusted (never user input).
CORPORA = {
    "brain_findings": {
        "id": "t.id::text", "kind": "finding",
        "text": "coalesce(t.issue,'') || ' — ' || coalesce(t.detail,'')",
        "where": "coalesce(t.issue,'') <> ''"},
    "brain_strategic_recommendations": {
        "id": "t.id::text", "kind": "recommendation",
        "text": "coalesce(t.title,'') || ' — ' || coalesce(t.spec_md,'')",
        "where": "coalesce(t.title,'') <> ''"},
    "news_articles": {
        "id": "t.id::text", "kind": "news",
        "text": "coalesce(t.title,'') || ' — ' || coalesce(t.summary,'')",
        "where": "coalesce(t.title,'') <> ''"},
    "deals": {
        "id": "t.id::text", "kind": "deal",
        "text": ("coalesce(t.buyer,'') || ' → ' || coalesce(t.seller,'') || ' (' || "
                 "coalesce(t.type,'') || ', ' || coalesce(t.market, t.region, '') || ') ' || "
                 "coalesce(t.notes,'')"),
        "where": "coalesce(t.buyer,'') <> '' OR coalesce(t.seller,'') <> ''"},
}


# ── auth ──────────────────────────────────────────────────────────────
def _admin_key():
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    exp = (_admin_key() or "").strip()
    if not exp:
        return False
    got = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, exp)


def _disabled() -> bool:
    return str(os.environ.get("BRAIN_RAG_DISABLED", "")).lower() in ("1", "true", "yes")


# ── DB ────────────────────────────────────────────────────────────────
def _db():
    import psycopg2
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not du:
        return None
    return psycopg2.connect(du, connect_timeout=8)


def _ensure() -> bool:
    """Lazy: pgvector extension + table. NEVER at boot (DDL-storm trap)."""
    c = _db()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS brain_corpus_embeddings (
                    id           SERIAL PRIMARY KEY,
                    source_table TEXT NOT NULL,
                    source_id    TEXT NOT NULL,
                    kind         TEXT,
                    text         TEXT,
                    embedding    vector({EMBED_DIM}),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_table, source_id)
                )
            """)
        c.commit()
        return True
    except Exception:
        try: c.rollback()
        except Exception: pass
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── Cohere embeddings ─────────────────────────────────────────────────
def _embed(texts, input_type="search_document"):
    key = (os.environ.get("COHERE_API_KEY") or "").strip()
    if not key or not texts:
        return None
    body = json.dumps({"texts": texts, "model": EMBED_MODEL,
                       "input_type": input_type, "truncate": "END"}).encode()
    req = urllib.request.Request("https://api.cohere.ai/v1/embed", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return d.get("embeddings")
    except Exception:
        return None


def _vec(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


# ── corpus selection ──────────────────────────────────────────────────
def _pending(cur, cap):
    """Rows across ALL registered corpora that don't yet have an embedding.
    A corpus whose columns don't resolve is skipped (rollback), never fatal."""
    rows = []
    for src, spec in CORPORA.items():
        if len(rows) >= cap:
            break
        lim = cap - len(rows)
        q = (f"SELECT '{src}', ({spec['id']}) AS sid, '{spec['kind']}', "
             f"left({spec['text']}, 1600) "
             f"FROM {src} t "
             f"LEFT JOIN brain_corpus_embeddings e "
             f"  ON e.source_table='{src}' AND e.source_id=({spec['id']}) "
             f"WHERE e.id IS NULL AND ({spec['where']}) "
             f"LIMIT {int(lim)}")
        try:
            cur.execute(q)
            rows += cur.fetchall()
        except Exception:
            try: cur.connection.rollback()
            except Exception: pass
    return rows


def _corpus_total(cur):
    total = 0
    for src, spec in CORPORA.items():
        try:
            cur.execute(f"SELECT count(*) FROM {src} t WHERE ({spec['where']})")
            total += cur.fetchone()[0] or 0
        except Exception:
            try: cur.connection.rollback()
            except Exception: pass
    return total


# ── retrieval (importable by any consumer) ────────────────────────────
def retrieve_context(query: str, k: int = 8, corpus: str = None) -> list:
    """Top-k most semantically-relevant rows, optionally scoped to one corpus
    (e.g. corpus='news_articles'). Returns [{source_table,source_id,kind,text,score}].
    Fail-soft → []."""
    if not query:
        return []
    qv = _embed([query], input_type="search_query")
    if not qv:
        return []
    qs = _vec(qv[0])
    c = _db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            if corpus:
                cs = list(corpus) if isinstance(corpus, (list, tuple)) else [corpus]
                cur.execute("""
                    SELECT source_table, source_id, kind, left(text, 500),
                           1 - (embedding <=> %s::vector)
                    FROM brain_corpus_embeddings WHERE source_table = ANY(%s)
                    ORDER BY embedding <=> %s::vector LIMIT %s
                """, (qs, cs, qs, int(k)))
            else:
                cur.execute("""
                    SELECT source_table, source_id, kind, left(text, 500),
                           1 - (embedding <=> %s::vector)
                    FROM brain_corpus_embeddings
                    ORDER BY embedding <=> %s::vector LIMIT %s
                """, (qs, qs, int(k)))
            return [{"source_table": r[0], "source_id": r[1], "kind": r[2],
                     "text": r[3], "score": round(float(r[4]), 4)} for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass


# ── endpoints ─────────────────────────────────────────────────────────
@brain_rag_bp.route("/api/v1/admin/brain/rag/reindex", methods=["POST", "GET"])
def reindex():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="BRAIN_RAG_DISABLED"), 200
    if not _ensure():
        return jsonify(ok=False, error="ensure_failed (pgvector/table)"), 200
    try:
        cap = max(1, min(1500, int(request.args.get("cap", "500"))))
    except Exception:
        cap = 500
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    embedded = 0
    try:
        with c.cursor() as cur:
            rows = _pending(cur, cap)
        for i in range(0, len(rows), _COHERE_BATCH):
            batch = rows[i:i + _COHERE_BATCH]
            vecs = _embed([r[3] or "" for r in batch], input_type="search_document")
            if not vecs or len(vecs) != len(batch):
                continue
            with c.cursor() as cur:
                for (st, sid, kind, text), vec in zip(batch, vecs):
                    cur.execute("""
                        INSERT INTO brain_corpus_embeddings
                          (source_table, source_id, kind, text, embedding)
                        VALUES (%s,%s,%s,%s,%s::vector)
                        ON CONFLICT (source_table, source_id) DO UPDATE
                          SET embedding=EXCLUDED.embedding, text=EXCLUDED.text, updated_at=NOW()
                    """, (st, sid, kind, text, _vec(vec)))
            c.commit()
            embedded += len(batch)
        with c.cursor() as cur:
            total = _corpus_total(cur)
            cur.execute("SELECT count(*) FROM brain_corpus_embeddings")
            emb = cur.fetchone()[0] or 0
        remaining = max(0, total - emb)
        return jsonify(ok=True, embedded=embedded, remaining=remaining,
                       done=(remaining == 0), model=EMBED_MODEL), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}", embedded=embedded), 200
    finally:
        try: c.close()
        except Exception: pass


@brain_rag_bp.route("/api/v1/admin/brain/rag/retrieve", methods=["GET"])
def retrieve():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(error="q required"), 400
    try:
        k = max(1, min(50, int(request.args.get("k", "8"))))
    except Exception:
        k = 8
    corpus = (request.args.get("corpus") or "").strip() or None
    return jsonify(ok=True, query=q, corpus=corpus, results=retrieve_context(q, k, corpus)), 200


@brain_rag_bp.route("/api/v1/admin/brain/rag/status", methods=["GET"])
def status():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            try:
                cur.execute("SELECT source_table, count(*) FROM brain_corpus_embeddings GROUP BY source_table")
                by = dict(cur.fetchall())
                cur.execute("SELECT max(updated_at) FROM brain_corpus_embeddings")
                last = cur.fetchone()[0]
            except Exception:
                c.rollback(); by = {}; last = None
            emb = sum(by.values())
            total = 0
            per = {}
            for src, spec in CORPORA.items():
                try:
                    cur.execute(f"SELECT count(*) FROM {src} t WHERE ({spec['where']})")
                    n = cur.fetchone()[0] or 0
                except Exception:
                    try: cur.connection.rollback()
                    except Exception: pass
                    n = 0
                total += n
                per[src] = f"{by.get(src, 0)}/{n}"
        return jsonify(ok=True, embedded=emb, corpus_total=total,
                       coverage_pct=round(100.0 * emb / max(1, total), 1),
                       by_corpus=per, last_indexed=str(last), model=EMBED_MODEL,
                       planner_wired=str(os.environ.get("BRAIN_RAG_ENABLED", "")).lower() in ("1", "true", "yes")), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    finally:
        try: c.close()
        except Exception: pass
