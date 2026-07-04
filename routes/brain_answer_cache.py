"""
brain_answer_cache.py — semantic ANSWER cache (PG/pgvector) + Haiku
number-verification pass for the flagship Q&A tools (deal_autopsy,
get_dchub_recommendation).

SEMANTIC ANSWER CACHE (Part A)
------------------------------
Postgres-backed — NOT in-process. Worker recycles wipe in-process caches
(proven live this week), so the cache lives in the same Neon PG the RAG
store uses:

    answer_cache(id, tool, tier, query_text, query_embedding vector(1024),
                 answer jsonb, created_at)

Lookup is pgvector cosine over the caller's query embedding. The query is
embedded via routes.brain_rag._embed with input_type='search_query' on BOTH
write and read — query<->query cosine is only meaningful when both sides use
the same asymmetric-embedding role.

A hit requires BOTH:
  · cosine >= ANSWER_CACHE_COSINE   (default 0.97 — near-duplicate questions
    only; anything looser starts answering different questions)
  · age    <  ANSWER_CACHE_TTL_S    (default 3600)

★ TIER-AWARE — NON-NEGOTIABLE: `tier` is part of the cache key. An anon/free
cached answer must NEVER serve a paid caller (they'd get the teaser) and a
paid cached answer must NEVER serve an anon caller (paywall bypass) —
cross-tier cache poisoning is a known trap in this codebase (grid_scoreboard
gating / mcp value-harness class). Tier strings are normalized (upper) so
"Pro" and "PRO" share one bucket but never leak across tiers.

Per-tool row cap ANSWER_CACHE_MAX_ROWS (default 500, delete-oldest).
Every path is fail-soft → cache miss / no-op; a broken cache can never break
the tool. Kill switch: ANSWER_CACHE_DISABLE=1.

Callers store POST-verification answers only: put_cached() runs AFTER the
Part-B verify pass has stripped/flagged, so a cached answer is never a
pre-verification one.

DDL is lazy (first use), raw psycopg2 (safe_db silently skips DDL), never at
boot (DDL-storm trap).

VERIFICATION PASS (Part B)
--------------------------
verify_answer(answer_text, facts_text): ONE cheap Haiku call — same model id
brain_rag's contextual-retrieval sidecar already uses repo-wide — through the
structured-outputs helper (routes.brain_llm_structured) with a strict
{ok, issues[]} schema. The check: does every NUMBER in the answer appear in
the structured SQL facts / retrieved evidence provided?

Returns:
  {"verified": True,  "ok": bool, "issues": [{"number","sentence"}]}
  {"verified": False, "reason": "disabled|over_budget|unparseable|<Exception>"}

On verifier failure/timeout (VERIFY_TIMEOUT_S cap, default 6s) callers serve
the answer UNVERIFIED with meta verified:false — verification is a quality
lift, never an availability risk. Budget: callers invoke it only for
paid-tier full answers; a per-process hour window caps calls at
VERIFY_MAX_PER_HOUR (default 60). Kill switch: ANSWER_VERIFY_DISABLE=1.

strip_flagged_sentences(text, issues) removes the offending sentence(s); if
stripping would blank the whole answer it flags instead (prefix caveat).
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

EMBED_DIM = 1024  # must match routes.brain_rag.EMBED_DIM (Cohere embed-v3)

# Same haiku id brain_rag's contextual-retrieval calls use (repo-wide id);
# prefix-matches brain_llm_structured's supported set ("claude-haiku-4-5").
VERIFY_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
_VERIFY_MAX_TOKENS = 500

_TRUTHY = ("1", "true", "yes", "on")


# ── env knobs (read at call time so a Railway env flip needs no deploy) ──
def _cache_disabled() -> bool:
    return (os.environ.get("ANSWER_CACHE_DISABLE") or "").strip().lower() in _TRUTHY


def _cosine_threshold() -> float:
    try:
        return float(os.environ.get("ANSWER_CACHE_COSINE", "0.97"))
    except Exception:
        return 0.97


def _ttl_s() -> int:
    try:
        return max(1, int(os.environ.get("ANSWER_CACHE_TTL_S", "3600")))
    except Exception:
        return 3600


def _max_rows() -> int:
    try:
        return max(1, int(os.environ.get("ANSWER_CACHE_MAX_ROWS", "500")))
    except Exception:
        return 500


def _verify_disabled() -> bool:
    return (os.environ.get("ANSWER_VERIFY_DISABLE") or "").strip().lower() in _TRUTHY


def _verify_cap() -> int:
    try:
        return max(0, int(os.environ.get("VERIFY_MAX_PER_HOUR", "60")))
    except Exception:
        return 60


def _verify_timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get("VERIFY_TIMEOUT_S", "6")))
    except Exception:
        return 6.0


def _verify_model() -> str:
    return (os.environ.get("ANSWER_VERIFY_MODEL") or "").strip() or VERIFY_MODEL_DEFAULT


# ── DB / embedding seams (module attrs so tests monkeypatch them) ─────
def _db():
    import psycopg2
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not du:
        return None
    return psycopg2.connect(du, connect_timeout=8)


def _embed_query(text: str):
    """Embed a cache-key query via the stable RAG API. input_type=
    'search_query' on BOTH read and write (query<->query cosine must be
    symmetric — mixing roles of Cohere's asymmetric embeddings would shift
    every score and silently disarm the 0.97 gate). None on any failure."""
    try:
        from routes.brain_rag import _embed
        vecs = _embed([text], input_type="search_query")
        return vecs[0] if vecs else None
    except Exception:
        return None


def _vec(v) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def _norm_tier(tier) -> str:
    """Tier is PART OF THE CACHE KEY. Normalize case so 'Pro'/'PRO' share a
    bucket; empty/None → '' which get/put treat as un-cacheable (an unknown
    tier must never fall into someone else's bucket)."""
    return str(tier or "").strip().upper()


_ENSURED = False


def _ensure(c) -> bool:
    """Lazy DDL on first use — raw psycopg2 (safe_db skips DDL), NEVER at
    boot (DDL-storm trap). Small btree only: with <=500 rows per tool a
    filtered seq scan beats a filtered ANN index (pgvector HNSW can
    under-return under WHERE filters)."""
    global _ENSURED
    if _ENSURED:
        return True
    try:
        with c.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS answer_cache (
                    id              SERIAL PRIMARY KEY,
                    tool            TEXT NOT NULL,
                    tier            TEXT NOT NULL,
                    query_text      TEXT,
                    query_embedding vector({EMBED_DIM}),
                    answer          JSONB,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS answer_cache_tool_tier_created
                ON answer_cache (tool, tier, created_at DESC)
            """)
        c.commit()
        _ENSURED = True
        return True
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        return False


# ── Part A: get / put ──────────────────────────────────────────────────
def get_cached(tool: str, tier, query: str):
    """Cached answer (dict) for a near-duplicate (tool, tier, query) — or
    None. Hit = cosine >= ANSWER_CACHE_COSINE AND age < ANSWER_CACHE_TTL_S.
    Every failure path (no key, embed down, DB down, bad row) is a MISS.
    NEVER raises."""
    try:
        if _cache_disabled():
            return None
        tier_s = _norm_tier(tier)
        q = (query or "").strip()
        if not tool or not tier_s or not q:
            return None
        emb = _embed_query(q)
        if not emb:
            return None
        c = None
        try:
            c = _db()
            if c is None or not _ensure(c):
                return None
            qs = _vec(emb)
            with c.cursor() as cur:
                # tool AND tier in the WHERE — the tier-isolation guarantee
                # lives HERE, not in ranking. A paid row is invisible to an
                # anon lookup and vice versa.
                cur.execute("""
                    SELECT answer,
                           1 - (query_embedding <=> %s::vector) AS cosine,
                           EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_s
                      FROM answer_cache
                     WHERE tool = %s AND tier = %s
                     ORDER BY query_embedding <=> %s::vector
                     LIMIT 1
                """, (qs, tool, tier_s, qs))
                row = cur.fetchone()
            if not row:
                return None
            answer, cosine, age_s = row[0], float(row[1] or 0.0), float(row[2] or 0.0)
            if cosine < _cosine_threshold():
                return None  # near-duplicate questions only
            if age_s >= _ttl_s():
                return None  # expired (rows are lazily evicted by put_cached's cap)
            if isinstance(answer, (str, bytes)):
                answer = json.loads(answer)
            return answer if isinstance(answer, dict) else None
        finally:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"answer_cache: get_cached failed (miss): {e}")
        return None


def put_cached(tool: str, tier, query: str, answer: dict) -> bool:
    """Store a POST-verification answer under (tool, tier, query-embedding).
    Enforces the per-tool row cap (delete-oldest). Fail-soft no-op → False.
    NEVER raises."""
    c = None
    try:
        if _cache_disabled():
            return False
        tier_s = _norm_tier(tier)
        q = (query or "").strip()
        if not tool or not tier_s or not q or not isinstance(answer, dict):
            return False
        emb = _embed_query(q)
        if not emb:
            return False
        c = _db()
        if c is None or not _ensure(c):
            return False
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO answer_cache (tool, tier, query_text, query_embedding, answer)
                VALUES (%s, %s, %s, %s::vector, %s::jsonb)
            """, (tool, tier_s, q[:2000], _vec(emb), json.dumps(answer, default=str)))
            # Per-tool row cap: keep the newest N rows for this tool (across
            # tiers), delete the rest — bounded storage, oldest-first out.
            cur.execute("""
                DELETE FROM answer_cache
                 WHERE tool = %s
                   AND id NOT IN (
                        SELECT id FROM answer_cache
                         WHERE tool = %s
                         ORDER BY created_at DESC, id DESC
                         LIMIT %s)
            """, (tool, tool, _max_rows()))
        c.commit()
        return True
    except Exception as e:
        try:
            c.rollback()
        except Exception:
            pass
        logger.warning(f"answer_cache: put_cached failed: {e}")
        return False
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


# ── Part B: verification pass ─────────────────────────────────────────
_VERIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "number": {"type": "string"},
                    "sentence": {"type": "string"},
                },
                "required": ["number", "sentence"],
            },
        },
    },
    "required": ["ok", "issues"],
}

_VERIFY_SYSTEM = (
    "You are a strict fact verifier for DC Hub. You get an ANSWER and the "
    "FACTS (structured SQL facts + retrieved evidence) it was composed from. "
    "Check every number in the ANSWER — money, MW, months, counts, "
    "percentages, years, prices. A number is SUPPORTED if it, or an "
    "equivalent formatting of it (e.g. $2.5B vs 2500000000, ~24mo vs "
    "24 months, 21,000+ vs 21000), appears in the FACTS. Report each "
    "UNSUPPORTED number together with the exact sentence of the ANSWER that "
    "contains it. Set ok=true only when every number in the ANSWER is "
    "supported by the FACTS."
)

# Per-process hour-window budget. In-process is fine for a SPEND cap (unlike
# the cache, where worker recycles would be a correctness bug): a recycle
# just resets the window early, and the cap bounds each worker's hour.
_verify_lock = threading.Lock()
_verify_window = [0, 0]  # [hour_bucket, calls_this_hour]


def _verify_budget_ok() -> bool:
    cap = _verify_cap()
    if cap <= 0:
        return False
    bucket = int(time.time() // 3600)
    with _verify_lock:
        if _verify_window[0] != bucket:
            _verify_window[0] = bucket
            _verify_window[1] = 0
        if _verify_window[1] >= cap:
            return False
        _verify_window[1] += 1
        return True


def _post_messages(body: dict, timeout: float) -> dict:
    """Raw POST to /v1/messages (urllib — same pattern as brain_rag's
    sidecars; no SDK dependency in the MCP process). Raises on any failure;
    verify_answer catches."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("no_anthropic_key")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


_DIGIT_RE = re.compile(r"\d")


def has_numbers(text) -> bool:
    return bool(_DIGIT_RE.search(text or ""))


def verify_answer(answer_text: str, facts_text: str) -> dict:
    """ONE cheap Haiku structured-outputs call: does every NUMBER in
    answer_text appear in facts_text? Returns
      {"verified": True,  "ok": bool, "issues": [{"number","sentence"}]}
      {"verified": False, "reason": ...}   (kill switch / budget / timeout /
                                            parse failure / any exception)
    Callers serve the answer UNVERIFIED (meta verified:false) whenever
    verified is False. NEVER raises."""
    try:
        if _verify_disabled():
            return {"verified": False, "reason": "disabled"}
        if not (answer_text or "").strip():
            return {"verified": False, "reason": "empty"}
        if not has_numbers(answer_text):
            # No numbers → nothing to fabricate; trivially ok, zero spend.
            return {"verified": True, "ok": True, "issues": [], "trivial": True}
        if not _verify_budget_ok():
            return {"verified": False, "reason": "over_budget"}

        from routes.brain_llm_structured import (build_messages_body,
                                                 parse_structured_json)
        user = (f"FACTS:\n{(facts_text or '')[:12000]}\n\n"
                f"ANSWER:\n{answer_text[:6000]}")
        body, applied = build_messages_body(
            _verify_model(), _VERIFY_SYSTEM,
            [{"role": "user", "content": user}],
            _VERIFY_MAX_TOKENS, schema=_VERIFY_SCHEMA)
        d = _post_messages(body, timeout=_verify_timeout_s())

        txt = ""
        for b in (d.get("content") or []):
            if isinstance(b, dict) and b.get("type") == "text":
                txt = b.get("text") or ""
                break
        parsed = parse_structured_json(txt)
        if parsed is None and txt:
            # Legacy free-text fallback (model fell off the structured path):
            # best-effort brace-slice parse, mirroring the repo's older sites.
            try:
                s, e = txt.find("{"), txt.rfind("}")
                parsed = json.loads(txt[s:e + 1]) if 0 <= s < e else None
                if not isinstance(parsed, dict):
                    parsed = None
            except Exception:
                parsed = None
        if not isinstance(parsed, dict) or "ok" not in parsed:
            return {"verified": False, "reason": "unparseable"}
        issues = [i for i in (parsed.get("issues") or []) if isinstance(i, dict)]
        return {"verified": True,
                "ok": bool(parsed.get("ok")) and not issues,
                "issues": issues}
    except Exception as e:
        # Timeout (6s cap) / network / auth / anything → serve unverified.
        return {"verified": False, "reason": type(e).__name__}


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WS_RE = re.compile(r"\s+")


def _norm_ws(s) -> str:
    return _WS_RE.sub(" ", (s or "")).strip().lower()


def strip_flagged_sentences(text: str, issues: list):
    """Remove the sentence(s) the verifier flagged as carrying unsupported
    numbers. Matching is defensive both ways (verifier may quote a fragment
    or a longer span) plus a raw number-containment backup. If stripping
    would blank the whole answer, FLAG instead of strip (prefix caveat) —
    an empty paid answer is worse than a caveated one.
    Returns (clean_text, stripped_count)."""
    if not text or not issues:
        return text, 0
    flagged_sents = [_norm_ws(i.get("sentence")) for i in issues
                     if isinstance(i, dict) and _norm_ws(i.get("sentence"))]
    flagged_nums = [str(i.get("number") or "").strip() for i in issues
                    if isinstance(i, dict) and str(i.get("number") or "").strip()]
    parts = _SENT_SPLIT_RE.split(text)
    keep, dropped = [], 0
    for s in parts:
        ns = _norm_ws(s)
        bad = False
        if ns:
            bad = any((fs in ns) or (ns in fs) for fs in flagged_sents)
            if not bad:
                bad = any(num in s for num in flagged_nums)
        if bad:
            dropped += 1
        else:
            keep.append(s)
    clean = " ".join(k for k in keep if k.strip()).strip()
    if not clean:
        return ("⚠️ [numbers in this answer could not be verified against "
                "source data] " + text), 0
    return clean, dropped


def _reset_for_tests():
    """Test hook: clear process-sticky state (DDL flag + verify budget)."""
    global _ENSURED
    _ENSURED = False
    with _verify_lock:
        _verify_window[0] = 0
        _verify_window[1] = 0
