"""
Phase FF+23-vectorize (2026-05-20) — Neon → Cloudflare Vectorize embedding sync.
============================================================================

Companion to routes/d1_sync.py. Builds the ANN index that powers
/api/v1/search/semantic in the Cloudflare Pages worker.

Pipeline per tick:
  1. Read facilities from Neon (same SQL as d1_sync, but only rows with
     enough text to be searchable — name + provider + location).
  2. Build a search blob per row:
       "{provider} {name} in {market or city}, {state}.
        {power_mw}MW {facility_type} - {status}"
  3. POST the blob batch to CF Workers AI
       /ai/run/@cf/baai/bge-small-en-v1.5
     → 384-dim float32 embedding per row (bge-small-en-v1.5 output).
  4. Upsert {id, vector, metadata} rows to Vectorize via
       /vectorize/v2/indexes/dchub-facility-search/upsert
     in batches of 100.

Idempotent: re-syncing the same row is an upsert (Vectorize merges by ID).

Cost note (per CLAUDE.md envelope):
  - Workers AI: ~21k embeddings × 10 neurons each = 210k neurons.
    Free tier is 10k/day so this exceeds free → ships on the paid
    tier ($0.011 per 1k neurons = ~$2.31 per full re-sync). Daily is
    overkill once steady-state; the cron is daily but most rows skip
    via the dedupe check at the top.
  - Vectorize: 21k × 384 ≈ 8M queried-dimensions per full query.
    Free tier is 30M/month → ~1 full re-sync's worth of search
    traffic fits free. Index storage 5M vectors free.

Env vars required (set on Railway):
  CLOUDFLARE_ACCOUNT_ID       4bb33ec40ef02f9f4b41dc97668d5a52
  CLOUDFLARE_API_TOKEN        Token with:
                                - Workers AI: Read
                                - Vectorize: Edit
                                (the existing token used for D1 sync
                                 needs these scopes added)

Endpoints:
  POST /api/v1/admin/vectorize-sync/run     Trigger a sync pass now
  GET  /api/v1/admin/vectorize-sync/status  Latest run stats + index size
"""
import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
vectorize_sync_bp = Blueprint("vectorize_sync", __name__)


# ── Auth (same pattern as d1_sync) ─────────────────────────────────
_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── Config ──────────────────────────────────────────────────────────
CF_ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID",
                              "4bb33ec40ef02f9f4b41dc97668d5a52")
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
VECTORIZE_INDEX = os.environ.get("VECTORIZE_INDEX_NAME", "dchub-facility-search")

CF_AI_URL = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}"
              f"/ai/run/@cf/baai/bge-small-en-v1.5")
CF_VEC_BASE = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}"
                f"/vectorize/v2/indexes/{VECTORIZE_INDEX}")

EMBED_BATCH = 25      # bge-small max ~25-50 strings per call (model-side)
UPSERT_BATCH = 100    # Vectorize: 100 vectors per upsert is the sweet spot
TIMEOUT_S = 1500      # whole job ceiling (25 min) — 21k rows / 25 per call
                      # × ~600ms each ≈ 8.5 min in practice
MAX_ROWS = 25000      # safety cap (we have ~21k facilities)


def _cf_post(path: str, body: dict, timeout: int = 30) -> dict:
    """POST to CF API. `path` is appended to CF_VEC_BASE for Vectorize,
    OR pass a full URL starting with http for arbitrary endpoints."""
    import requests
    if not CF_TOKEN:
        raise RuntimeError(
            "CLOUDFLARE_API_TOKEN not set — Vectorize sync disabled. "
            "Token needs Workers AI:Read + Vectorize:Edit scopes.")
    url = path if path.startswith("http") else (CF_VEC_BASE + path)
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {CF_TOKEN}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _cf_get(path: str, timeout: int = 15) -> dict:
    import requests
    url = path if path.startswith("http") else (CF_VEC_BASE + path)
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {CF_TOKEN}"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _neon_facilities() -> list:
    """Read facility rows from Neon. Mirrors d1_sync filters but adds
    a `text_hash` column so we can skip rows whose searchable text
    hasn't changed since the last sync."""
    try:
        from main import get_db
    except Exception:
        return []
    conn = get_db()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT df.id, df.name, df.provider, df.city, df.state,
                       df.market, df.facility_type, df.status,
                       COALESCE(df.power_mw, f.power_mw) AS power_mw
                FROM discovered_facilities df
                LEFT JOIN facilities f ON f.id = df.merged_facility_id
                WHERE COALESCE(df.is_duplicate, 0) = 0
                  AND df.name IS NOT NULL
                  AND length(df.name) >= 3
                ORDER BY COALESCE(df.power_mw, f.power_mw) DESC NULLS LAST
                LIMIT %s
            """, (MAX_ROWS,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        try: conn.close()
        except Exception: pass


def _build_text(row: dict) -> str:
    """Build the searchable text blob for a facility row. Order matters
    for embedding quality: provider/name first (highest signal), then
    location, then specs. bge-small-en handles ~512 tokens; we stay
    well under."""
    name = (row.get("name") or "").strip()
    provider = (row.get("provider") or "").strip()
    loc = (row.get("market") or row.get("city") or "").strip()
    state = (row.get("state") or "").strip()
    ftype = (row.get("facility_type") or "data center").strip()
    status = (row.get("status") or "").strip()
    mw = row.get("power_mw")

    parts = []
    if provider and name:
        parts.append(f"{provider} {name}")
    elif name:
        parts.append(name)
    elif provider:
        parts.append(f"{provider} facility")
    else:
        return ""  # no useful text — skip

    if loc and state:
        parts.append(f"in {loc}, {state}")
    elif loc:
        parts.append(f"in {loc}")
    elif state:
        parts.append(f"in {state}")

    specs = []
    if mw:
        try:
            specs.append(f"{float(mw):.0f}MW")
        except (TypeError, ValueError):
            pass
    if ftype:
        specs.append(ftype.lower())
    if specs:
        parts.append(" ".join(specs))

    if status:
        parts.append(f"- {status.lower()}")

    return ". ".join(parts).strip()


def _text_hash(text: str) -> str:
    """Stable short hash for dedupe."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _embed_batch(texts: list) -> list:
    """POST texts → 384-dim embeddings. Returns list aligned with input."""
    resp = _cf_post(CF_AI_URL, {"text": texts}, timeout=60)
    # CF AI response shape: { result: { data: [[...384 floats...], ...], shape: [n, 384] }, success: true }
    res = resp.get("result") or {}
    data = res.get("data") or []
    if len(data) != len(texts):
        raise RuntimeError(
            f"embedding count mismatch: sent {len(texts)} got {len(data)}")
    return data


def _vec_upsert(vectors: list) -> dict:
    """Upsert vectors to Vectorize. `vectors` is a list of
       {id, values: [384 floats], metadata: {...}} dicts.

    Vectorize v2 upsert accepts NDJSON via the body. Each line is a
    JSON-encoded vector object."""
    body = "\n".join(json.dumps(v) for v in vectors)
    import requests
    if not CF_TOKEN:
        raise RuntimeError("CLOUDFLARE_API_TOKEN missing for Vectorize upsert")
    r = requests.post(
        f"{CF_VEC_BASE}/upsert",
        headers={
            "Authorization": f"Bearer {CF_TOKEN}",
            "Content-Type": "application/x-ndjson",
        },
        data=body,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _run_sync(force: bool = False) -> dict:
    """One full sync pass.

    `force=True` re-embeds every row even if text_hash unchanged. Use
    this once after a model upgrade or schema change."""
    started = time.time()
    out = {
        "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "rows_read": 0,
        "rows_embedded": 0,
        "rows_skipped_unchanged": 0,
        "rows_skipped_empty": 0,
        "vectors_upserted": 0,
        "embed_calls": 0,
        "upsert_calls": 0,
        "errors": [],
    }
    if not CF_TOKEN:
        out["errors"].append("CLOUDFLARE_API_TOKEN not configured")
        return out

    rows = _neon_facilities()
    out["rows_read"] = len(rows)
    if not rows:
        out["errors"].append("zero rows from Neon — Railway DB or schema issue")
        return out

    # Build prior-hash map from D1 if available (so we only re-embed
    # changed rows). For the first run, D1 won't have hashes yet → all
    # rows get embedded. After that, this caps daily cost to deltas.
    prior_hashes = {}
    if not force:
        try:
            prior_hashes = _load_prior_hashes()
        except Exception as e:
            out["errors"].append(f"prior-hash load failed (will embed all): {str(e)[:120]}")

    # Stream through rows in batches
    queue_vectors = []  # buffer for upserts
    queue_text = []     # buffer for embed calls
    queue_meta = []     # parallel buffer: (id, metadata, text_hash)

    def _flush_embed():
        nonlocal queue_text, queue_meta, queue_vectors
        if not queue_text:
            return
        try:
            embs = _embed_batch(queue_text)
            out["embed_calls"] += 1
        except Exception as e:
            out["errors"].append(f"embed_batch failed (n={len(queue_text)}): {str(e)[:160]}")
            queue_text, queue_meta = [], []
            return
        for emb, (rid, meta, h) in zip(embs, queue_meta):
            queue_vectors.append({
                "id": str(rid),
                "values": emb,
                "metadata": dict(meta, text_hash=h),
            })
            out["rows_embedded"] += 1
        queue_text, queue_meta = [], []

    def _flush_upsert():
        nonlocal queue_vectors
        if not queue_vectors:
            return
        try:
            _vec_upsert(queue_vectors)
            out["vectors_upserted"] += len(queue_vectors)
            out["upsert_calls"] += 1
        except Exception as e:
            out["errors"].append(
                f"vectorize_upsert failed (n={len(queue_vectors)}): {str(e)[:160]}")
        queue_vectors = []

    for r in rows:
        if time.time() - started > TIMEOUT_S:
            out["errors"].append(
                f"timeout after {TIMEOUT_S}s — embedded {out['rows_embedded']}, "
                f"upserted {out['vectors_upserted']}")
            break
        text = _build_text(r)
        if not text:
            out["rows_skipped_empty"] += 1
            continue
        h = _text_hash(text)
        rid = r.get("id")
        if rid is None:
            out["rows_skipped_empty"] += 1
            continue
        if not force and prior_hashes.get(str(rid)) == h:
            out["rows_skipped_unchanged"] += 1
            continue

        meta = {
            "provider": (r.get("provider") or "")[:80],
            "state": (r.get("state") or "")[:8],
            "market": (r.get("market") or "")[:80],
            "facility_type": (r.get("facility_type") or "")[:48],
        }
        # power_mw: Vectorize metadata supports numeric filters
        try:
            if r.get("power_mw") is not None:
                meta["power_mw"] = float(r["power_mw"])
        except (TypeError, ValueError):
            pass

        queue_text.append(text)
        queue_meta.append((rid, meta, h))

        if len(queue_text) >= EMBED_BATCH:
            _flush_embed()
        if len(queue_vectors) >= UPSERT_BATCH:
            _flush_upsert()

    # Drain
    _flush_embed()
    _flush_upsert()

    # Persist hashes for next-run dedupe (best effort)
    try:
        if out["rows_embedded"] > 0:
            _save_hashes_for_embedded(rows, out)
    except Exception as e:
        out["errors"].append(f"hash persist failed: {str(e)[:120]}")

    out["ok"] = out["vectors_upserted"] > 0 or out["rows_skipped_unchanged"] > 0
    out["duration_seconds"] = round(time.time() - started, 2)
    out["finished_at"] = datetime.now(timezone.utc).isoformat()
    return out


# ── Hash persistence: a small text_hash column on Neon side ─────────
# We add a tiny support table so subsequent runs only re-embed changed
# rows. Falls back to "re-embed all" if the table doesn't exist.

def _ensure_hash_table():
    try:
        from main import get_db
    except Exception:
        return False
    conn = get_db()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vectorize_sync_hashes (
                    facility_id TEXT PRIMARY KEY,
                    text_hash   TEXT NOT NULL,
                    synced_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()
        return True
    except Exception:
        return False
    finally:
        try: conn.close()
        except Exception: pass


def _load_prior_hashes() -> dict:
    if not _ensure_hash_table():
        return {}
    try:
        from main import get_db
    except Exception:
        return {}
    conn = get_db()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT facility_id, text_hash FROM vectorize_sync_hashes")
            return {fid: th for fid, th in cur.fetchall()}
    finally:
        try: conn.close()
        except Exception: pass


def _save_hashes_for_embedded(rows: list, out: dict):
    """Re-derive the hash for each row and upsert into Postgres."""
    try:
        from main import get_db
    except Exception:
        return
    conn = get_db()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            tuples = []
            for r in rows:
                rid = r.get("id")
                if rid is None:
                    continue
                t = _build_text(r)
                if not t:
                    continue
                tuples.append((str(rid), _text_hash(t)))
            if tuples:
                # psycopg2.extras.execute_values would be faster; this is
                # fine for 21k rows on a daily cron.
                cur.executemany(
                    "INSERT INTO vectorize_sync_hashes (facility_id, text_hash, synced_at) "
                    "VALUES (%s, %s, NOW()) "
                    "ON CONFLICT (facility_id) DO UPDATE SET "
                    "  text_hash = EXCLUDED.text_hash, synced_at = NOW()",
                    tuples
                )
                conn.commit()
    finally:
        try: conn.close()
        except Exception: pass


# ── Endpoints ───────────────────────────────────────────────────────
@vectorize_sync_bp.route("/api/v1/admin/vectorize-sync/run", methods=["POST"])
def run_now():
    """Admin: trigger a vectorize sync pass.

    Query params:
      force=1   re-embed every row regardless of text_hash dedup
    """
    if not _admin_ok():
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    result = _run_sync(force=force)
    return jsonify(result), (200 if result["ok"] else 500)


@vectorize_sync_bp.route("/api/v1/admin/vectorize-sync/status", methods=["GET"])
def status():
    """Public: index size + binding sanity check (no auth — diagnostic)."""
    if not CF_TOKEN:
        return jsonify(
            ok=False,
            error="CLOUDFLARE_API_TOKEN not set on Railway",
            hint="Set token with Workers AI:Read + Vectorize:Edit scopes",
        ), 503

    out = {
        "ok": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "index": VECTORIZE_INDEX,
        "account": CF_ACCOUNT,
    }
    try:
        info = _cf_get("")  # GET CF_VEC_BASE → index metadata
        out["index_info"] = info.get("result") or info
    except Exception as e:
        out["ok"] = False
        out["index_info_error"] = str(e)[:200]

    # Row-count proxy: count hashes we have stored (one per embedded
    # facility). Cheap; doesn't query Vectorize.
    try:
        from main import get_db
        conn = get_db()
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM vectorize_sync_hashes")
                    out["embedded_rows_known"] = cur.fetchone()[0]
            finally:
                conn.close()
    except Exception as e:
        out["embedded_rows_error"] = str(e)[:120]

    return jsonify(out)


# Module-load smoke
def _smoke():
    if not CF_TOKEN:
        logger.warning(
            "[vectorize-sync] CLOUDFLARE_API_TOKEN not set — sync will fail. "
            "Token needs Workers AI:Read + Vectorize:Edit scopes.")
    else:
        logger.info("[vectorize-sync] ready, account=%s index=%s",
                     CF_ACCOUNT, VECTORIZE_INDEX)

_smoke()
