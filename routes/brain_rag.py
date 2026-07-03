"""
brain_rag.py — brain context-assembly RAG (pgvector + Cohere embeddings).

The strategic planner re-pulls ~11 HTTP sources every synthesis but has no memory
of its OWN prior findings + recommendations beyond a few recent rows. This gives
it semantic RECALL: embed the brain corpus (brain_findings + brain_strategic_
recommendations) once, then retrieve the top-k most RELEVANT prior items for the
focus at hand. Numbers stay in SQL — this is only for the UNSTRUCTURED corpus
(finding/rec prose). Owner-greenlit 2026-07-03.

Store:  brain_corpus_embeddings(source_table, source_id, kind, text,
        embedding vector(1024))  — pgvector on Neon.
Embed:  Cohere embed-english-v3.0 (1024-d, asymmetric search_document/search_query;
        OpenAI account is out of quota, so Cohere not OpenAI). Batched ≤96/call.
Recall: cosine (<=>). Wired into brain_strategic_planner behind BRAIN_RAG_ENABLED.

Endpoints (admin, X-Admin-Key):
  POST /api/v1/admin/brain/rag/reindex?cap=300   — embed up to cap not-yet-embedded rows
  GET  /api/v1/admin/brain/rag/retrieve?q=...&k=8 — test retrieval
  GET  /api/v1/admin/brain/rag/status            — coverage counts
Kill: BRAIN_RAG_DISABLED=1 (endpoints) / BRAIN_RAG_ENABLED unset (planner stays as-is).
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
    """Return list of 1024-float vectors (or None). input_type: search_document
    for the corpus, search_query for a lookup — Cohere v3 is asymmetric."""
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
    """Rows in the brain corpus that don't yet have an embedding."""
    rows = []
    cur.execute("""
        SELECT 'brain_findings', bf.id::text, 'finding',
               left(coalesce(bf.issue,'') || ' — ' || coalesce(bf.detail,''), 1600)
        FROM brain_findings bf
        LEFT JOIN brain_corpus_embeddings e
          ON e.source_table='brain_findings' AND e.source_id=bf.id::text
        WHERE e.id IS NULL AND coalesce(bf.issue,'') <> ''
        ORDER BY bf.id DESC
        LIMIT %s
    """, (cap,))
    rows += cur.fetchall()
    if len(rows) < cap:
        cur.execute("""
            SELECT 'brain_strategic_recommendations', r.id::text, 'recommendation',
                   left(coalesce(r.title,'') || ' — ' || coalesce(r.spec_md,''), 1600)
            FROM brain_strategic_recommendations r
            LEFT JOIN brain_corpus_embeddings e
              ON e.source_table='brain_strategic_recommendations' AND e.source_id=r.id::text
            WHERE e.id IS NULL AND coalesce(r.title,'') <> ''
            ORDER BY r.id DESC
            LIMIT %s
        """, (cap - len(rows),))
        rows += cur.fetchall()
    return rows


# ── retrieval (the payoff — importable by the planner) ────────────────
def retrieve_context(query: str, k: int = 8) -> list:
    """Top-k most semantically-relevant prior findings + recommendations.
    Returns [{source_table, source_id, kind, text, score}]. Fail-soft → []."""
    if not query:
        return []
    qv = _embed([query], input_type="search_query")
    if not qv:
        return []
    c = _db()
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute(f"""
                SELECT source_table, source_id, kind, left(text, 500),
                       1 - (embedding <=> %s::vector) AS score
                FROM brain_corpus_embeddings
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """, (_vec(qv[0]), _vec(qv[0]), int(k)))
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
        cap = max(1, min(1000, int(request.args.get("cap", "300"))))
    except Exception:
        cap = 300
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
            cur.execute("""
                SELECT (SELECT count(*) FROM brain_findings WHERE coalesce(issue,'')<>'')
                     + (SELECT count(*) FROM brain_strategic_recommendations WHERE coalesce(title,'')<>'')
                     - (SELECT count(*) FROM brain_corpus_embeddings)
            """)
            remaining = max(0, cur.fetchone()[0] or 0)
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
    return jsonify(ok=True, query=q, results=retrieve_context(q, k)), 200


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
                cur.execute("SELECT count(*), max(updated_at) FROM brain_corpus_embeddings")
                emb, last = cur.fetchone()
            except Exception:
                c.rollback(); emb, last = 0, None
            cur.execute("SELECT count(*) FROM brain_findings WHERE coalesce(issue,'')<>''")
            f = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM brain_strategic_recommendations WHERE coalesce(title,'')<>''")
            r = cur.fetchone()[0]
        return jsonify(ok=True, embedded=emb, corpus_total=f + r,
                       coverage_pct=round(100.0 * emb / max(1, f + r), 1),
                       last_indexed=str(last), model=EMBED_MODEL,
                       planner_wired=str(os.environ.get("BRAIN_RAG_ENABLED", "")).lower() in ("1", "true", "yes")), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    finally:
        try: c.close()
        except Exception: pass
