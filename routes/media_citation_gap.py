"""media_citation_gap.py — Citation-Gap Content (FEATURE 4, 2026-06-23).

GOAL
====
Close the 0% share-of-voice by targeting the EXACT questions DC Hub loses.

The citation hunter (routes/citation_hunter.py) probes Claude daily with a
battery of data-center questions and records, per query, whether DC Hub was
mentioned (`dchub_mentioned`) and which competitors were (DCD / JLL / CBRE /
DCK / Frontier, in `citation_probes.competitors_mentioned`). The LOST QUERIES
are the rows where a competitor IS cited but DC Hub is NOT — the precise
questions where rivals own the answer and we are invisible.

This module reads those lost queries and, for each, drafts a concise,
data-backed, LLM-optimized answer (a citable answer to that exact question,
built from VERIFIED DCPI + canonical counts) and WRITES IT TO A REVIEW QUEUE
(`media_citation_content_queue`) for the operator to approve. It NEVER
publishes a page, NEVER posts to a network, NEVER emails anyone.

HARD SAFETY CONTRACT (the operator just had an auto-post disparage a partner —
safety is paramount):
  1. DARK BY DEFAULT. The whole feature is gated behind MEDIA_CITATION_GAP_ENABLED.
     With the flag off, every endpoint returns {ok:true, skipped:"disabled"} and
     the module does NOTHING (no DB, no LLM, no network).
  2. NO AUTO-SEND / NO AUTO-PUBLISH. Every draft lands in the review queue with
     status='draft'. /approve flips status to 'approved' (an operator decision
     record) — it does NOT publish anything anywhere. Publishing stays a separate,
     human, out-of-band step.
  3. EVERY draft is routed through BOTH hardened guards before it is queued:
       - content_publisher._should_skip_publish(cur, text, platform)  (the
         partner-disparagement / quality / zero-stat / dedup gate)
       - routes.media_claim_verify.verify_claims(text)                (the
         numeric-claim fact-check vs canonical_stats)
     Anything either guard flags is DROPPED (recorded as 'rejected' with the
     reason) and never queued as a draft.
  4. VERIFIED NUMBERS ONLY. Figures come from live data: DCPI verdict /
     excess_power_score / constraint_score / time_to_power_months from the
     market_power_scores table, and counts from canonical_stats.get_canonical_stats().
     A value that can't be read live is OMITTED — never fabricated. The LLM is
     handed only verified facts and explicitly forbidden to invent any number,
     and the claim-verifier is the backstop.
  5. SELF-IMPORT-SAFE. This module imports cleanly with the flag off and no
     special env — every optional import / DB / LLM call is wrapped, so
     registering the blueprint can NEVER crash app boot.

ADMIN ENDPOINTS (all admin-gated, mirror brain_innovation._innov_admin_ok):
  POST /api/v1/media/citation-gap/run            find lost queries + draft answers
  GET  /api/v1/media/citation-gap/queue          list the review queue
  POST /api/v1/media/citation-gap/approve/<id>   operator approves a draft (no publish)

Kill-switch: MEDIA_CITATION_GAP_ENABLED  (unset / 0 / false = OFF = dark)
"""
from __future__ import annotations

from utils.anthropic_helper import cached_system
import os
import json
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

media_citation_gap_bp = Blueprint("media_citation_gap", __name__)

# Platform label used for the guard call. content_publisher applies its
# LinkedIn-only number-lead + claim-verify gates when platform == 'linkedin';
# this is page content, so we use a distinct label and run the claim-verifier
# OURSELVES (below) so the fact-check applies regardless of platform.
_GUARD_PLATFORM = "citation_answer"

# Competitor names as they appear in citation_probes.competitors_mentioned
# (written by citation_hunter._COMPETITOR_PATTERNS). Used only to describe the
# gap; never written into draft copy (we don't name/disparage competitors).
_COMPETITORS = {"DCHawk", "dcByte", "DCK", "DCD", "Frontier", "CBRE", "JLL"}


# ── kill switch ──────────────────────────────────────────────────────────────
def _enabled() -> bool:
    """DARK BY DEFAULT. Feature is OFF unless the flag is explicitly truthy."""
    return str(os.environ.get("MEDIA_CITATION_GAP_ENABLED", "")).strip().lower() \
        in ("1", "true", "yes", "on")


def _disabled_response():
    """Uniform no-op response when the feature is dark."""
    return jsonify(ok=True, skipped="disabled",
                   flag="MEDIA_CITATION_GAP_ENABLED"), 200


# ── admin gate (mirrors brain_innovation._innov_admin_ok) ─────────────────────
def _admin_ok() -> bool:
    _keys = set()
    for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY", "ADMIN_KEY"):
        _v = os.environ.get(_n)
        if _v:
            _keys.add(_v)
    if not _keys:
        # No admin key configured anywhere → fail CLOSED (deny), so this
        # write-capable surface is never accidentally open.
        return False
    _sent = (request.headers.get("X-Internal-Key")
             or request.headers.get("X-Admin-Key")
             or request.args.get("admin_key") or "").strip()
    return bool(_sent) and _sent in _keys


# ── DB (autocommit; mirrors citation_hunter._conn) ────────────────────────────
def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[citation_gap] db connect failed: %s", str(e)[:120])
        return None


# Review-queue schema. ALTER-friendly column adds (CCC pattern from
# ai_citation_tracker) so a prior partial-schema state self-heals. status:
# draft = awaiting operator | approved = operator OK'd (NOT published) |
# rejected = a guard dropped it (reason recorded). publish stays a human step.
_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS media_citation_content_queue (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
_QUEUE_COLUMNS = [
    ("created_at",       "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("query",            "TEXT NOT NULL DEFAULT ''"),     # the lost question
    ("competitors",      "JSONB DEFAULT '[]'::jsonb"),    # who was cited instead
    ("draft_title",      "TEXT"),
    ("draft_body",       "TEXT"),
    ("verified_facts",   "JSONB DEFAULT '{}'::jsonb"),    # the facts handed to the LLM
    ("status",           "TEXT NOT NULL DEFAULT 'draft'"),  # draft|approved|rejected
    ("reject_reason",    "TEXT"),
    ("source",           "TEXT DEFAULT 'citation_gap_run'"),
    ("approved_at",      "TIMESTAMPTZ"),
    ("approved_by",      "TEXT"),
]
_QUEUE_INDEXES = [
    ("ix_citation_gap_status",  "media_citation_content_queue(status, created_at DESC)"),
    ("ix_citation_gap_query",   "media_citation_content_queue(query)"),
]


def _ensure_queue(c) -> bool:
    """Idempotent schema init (autocommit; per-statement guarded)."""
    try:
        with c.cursor() as cur:
            try:
                cur.execute(_QUEUE_DDL)
            except Exception as e:
                logger.warning("[citation_gap] CREATE TABLE: %s", str(e)[:120])
            for col, ddl in _QUEUE_COLUMNS:
                try:
                    cur.execute(
                        f"ALTER TABLE media_citation_content_queue "
                        f"ADD COLUMN IF NOT EXISTS {col} {ddl}")
                except Exception as e:
                    logger.warning("[citation_gap] ALTER %s: %s", col, str(e)[:120])
            for name, ddl in _QUEUE_INDEXES:
                try:
                    cur.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {ddl}")
                except Exception as e:
                    logger.warning("[citation_gap] INDEX %s: %s", name, str(e)[:120])
        return True
    except Exception as e:
        logger.warning("[citation_gap] ensure_queue failed: %s", str(e)[:160])
        return False


# ── find the lost queries ─────────────────────────────────────────────────────
def _find_lost_queries(c, days: int = 30, limit: int = 25) -> list[dict]:
    """Lost query = a recent citation probe where DC Hub was NOT mentioned but a
    competitor WAS. Reads citation_probes (written by citation_hunter). Dedups by
    query, keeping the most recent observation. Never raises — returns [] on any
    failure so a run can't crash on a missing/old table."""
    out: list[dict] = []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (query)
                       query, competitors_mentioned, probe_date
                  FROM citation_probes
                 WHERE probe_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
                   AND COALESCE(dchub_mentioned, FALSE) = FALSE
                   AND competitors_mentioned IS NOT NULL
                   AND competitors_mentioned <> '[]'::jsonb
                 ORDER BY query, probe_date DESC
            """, (int(days),))
            rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("[citation_gap] find_lost_queries failed: %s", str(e)[:160])
        return out

    for r in rows:
        comps = r.get("competitors_mentioned")
        if isinstance(comps, str):
            try:
                comps = json.loads(comps)
            except Exception:
                comps = []
        if not isinstance(comps, list):
            comps = []
        # Only keep genuine competitor citations (defensive intersection).
        comps = [x for x in comps if x in _COMPETITORS] or comps
        if not comps:
            continue
        out.append({
            "query": (r.get("query") or "").strip(),
            "competitors": comps,
            "probe_date": (r["probe_date"].isoformat()
                           if r.get("probe_date") else None),
        })
        if len(out) >= int(limit):
            break
    return out


# ── verified facts (NEVER fabricate) ──────────────────────────────────────────
def _canonical_facts() -> dict:
    """Live canonical counts as a phrase-ready dict. Floors round DOWN (never
    over-claim). Returns {} on any failure so a fact simply gets omitted."""
    try:
        import canonical_stats as cs
        s = cs.get_canonical_stats() or {}
        facts: dict = {}
        # Use the phrase helpers when available (they floor conservatively).
        try:
            facts["facilities_tracked"] = cs.facilities_phrase()
            facts["facilities_verified"] = cs.facilities_verified_phrase()
            facts["countries"] = cs.countries_phrase()
            facts["markets"] = cs.markets_phrase()
            facts["grid_coverage"] = cs.grid_coverage_phrase("short")
        except Exception:
            # Raw ints as a fallback; only include positive, real values.
            for k in ("facilities", "countries", "markets"):
                v = s.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    facts[k] = int(v)
        return facts
    except Exception as e:
        logger.warning("[citation_gap] canonical_facts failed: %s", str(e)[:140])
        return {}


def _top_build_markets(c, limit: int = 5) -> list[dict]:
    """Verified DCPI verdicts straight from market_power_scores. Same columns +
    composite the comprehensive report uses (excess_power_score - constraint_score).
    Returns [] on any failure — a missing market is omitted, never invented."""
    out: list[dict] = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT market_name, state, iso, verdict,
                       excess_power_score, constraint_score, time_to_power_months
                  FROM market_power_scores
                 WHERE verdict = 'BUILD' AND COALESCE(published, TRUE) = TRUE
                 ORDER BY (COALESCE(excess_power_score,0) - COALESCE(constraint_score,0)) DESC
                 LIMIT %s
            """, (int(limit),))
            for r in cur.fetchall() or []:
                # Only emit numbers that are actually present (no zero-fill that
                # would read as a fabricated score).
                rec = {"market": r[0], "state": r[1], "iso": r[2], "verdict": r[3]}
                if r[4] is not None:
                    rec["excess_power_score"] = round(float(r[4]), 1)
                if r[5] is not None:
                    rec["constraint_score"] = round(float(r[5]), 1)
                if r[6] is not None:
                    rec["time_to_power_months"] = round(float(r[6]), 1)
                out.append(rec)
    except Exception as e:
        logger.warning("[citation_gap] top_build_markets failed: %s", str(e)[:160])
    return out


def _gather_verified_facts(c) -> dict:
    """The ONLY facts the LLM is allowed to use. All live; omit what we can't read."""
    facts = {"canonical": _canonical_facts(),
             "build_markets": _top_build_markets(c, limit=5)}
    # Strip empty sections so the prompt never implies a fact we don't have.
    return {k: v for k, v in facts.items() if v}


# ── LLM draft (urllib pattern from linkedin_content_engine) ───────────────────
_DRAFT_SYSTEM = (
    "You are a senior data-center infrastructure analyst writing a concise, "
    "citable reference answer for an LLM-readable knowledge page. Answer the "
    "exact question directly and factually. HONESTY IS NON-NEGOTIABLE: use ONLY "
    "the VERIFIED FACTS provided; do NOT invent or estimate any number, market, "
    "percentage, dollar figure, capacity, or date that is not in those facts. If "
    "you lack a specific value, describe the category without a number. Do NOT "
    "name, compare against, rank against, or disparage any competitor or other "
    "company — answer on DC Hub's own merits only. Neutral, specific, no hype, no "
    "marketing slogans. End with one neutral source line: 'Source: DC Hub "
    "(dchub.cloud)'."
)


def _draft_answer(query: str, facts: dict) -> tuple[str | None, str | None]:
    """Ask Sonnet for a draft answer to one lost query, using ONLY `facts`.
    Returns (text, error). Best-effort: any failure returns (None, error) so the
    caller skips this query instead of crashing the run."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None, "no_anthropic_api_key"
    if not facts:
        # Refuse to draft with zero verified facts — that's exactly how an
        # unverifiable number would sneak in.
        return None, "no_verified_facts"
    try:
        from utils.anthropic_helper import anthropic_messages_url
    except Exception as e:
        return None, f"helper_unavailable:{str(e)[:60]}"

    user_prompt = (
        f"QUESTION TO ANSWER:\n{query}\n\n"
        f"VERIFIED FACTS (the ONLY facts you may use — do not add any number "
        f"not present here):\n{json.dumps(facts, default=str)[:1800]}\n\n"
        "Write a direct, factual answer (max ~180 words). Open with the single "
        "most relevant verified fact. Mention DC Hub's MCP / live data access "
        "where natural. Do NOT fabricate any figure. Do NOT mention competitors."
    )
    body = json.dumps({
        "model": "claude-sonnet-4-5",
        "max_tokens": 700,
        "system": cached_system(_DRAFT_SYSTEM),
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")
    try:
        import urllib.request
        req = urllib.request.Request(
            anthropic_messages_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": "dchub-brain/1.0",
                "Anthropic-Version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            payload = json.loads(r.read().decode("utf-8"))
        parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if text.startswith('"') and text.endswith('"') and len(text) > 10:
            text = text[1:-1].strip()
        return (text or None), (None if text else "empty_response")
    except Exception as e:
        return None, f"{type(e).__name__}:{str(e)[:80]}"


def _draft_title(query: str) -> str:
    """A plain, factual page title from the question. No fabricated specifics."""
    q = (query or "").strip().rstrip("?").strip()
    return (q[:117] + "...") if len(q) > 120 else (q or "Data center reference answer")


# ── the guards (BOTH must pass) ───────────────────────────────────────────────
def _run_guards(c, text: str) -> tuple[bool, str]:
    """Route a draft through BOTH hardened guards. Returns (ok, reason).
    ok=False means DROP the draft (reason explains which guard, never queued).

    Guard 1: content_publisher._should_skip_publish — partner-disparagement /
             LLM-disclaimer / quality / zero-stat / dedup. Returns (skip, why).
    Guard 2: media_claim_verify.verify_claims — numeric claims vs canonical_stats.
             Returns {ok, blocks, warns}; any block drops the draft.

    Fail-CLOSED for THIS feature: if a guard is unavailable or errors, we DROP
    the draft (the operator can re-run later). For an autonomous content pipeline
    that nearly shipped a partner-disparaging post, a missed draft is strictly
    safer than an unchecked one. (Note: this is the opposite of the publisher's
    own fail-OPEN posture, which is appropriate for an already-vetted live feed;
    here nothing is vetted yet.)"""
    if not text or not text.strip():
        return False, "empty_draft"

    # Guard 1 — disparagement / quality / dedup.
    try:
        from content_publisher import _should_skip_publish
        skip, why = _should_skip_publish(c.cursor(), text, _GUARD_PLATFORM)
        if skip:
            return False, f"should_skip_publish: {why or 'flagged'}"
    except Exception as e:
        return False, f"guard1_unavailable:{str(e)[:80]}"

    # Guard 2 — numeric claim fact-check.
    try:
        from routes.media_claim_verify import verify_claims
        cv = verify_claims(text)
        if cv.get("blocks"):
            return False, "claim_verify: " + "; ".join(cv["blocks"])[:200]
    except Exception as e:
        return False, f"guard2_unavailable:{str(e)[:80]}"

    return True, ""


# ── core run: find gaps + draft + queue ───────────────────────────────────────
def _run(days: int, limit: int) -> dict:
    out = {"ok": True, "checked": 0, "queued": 0, "dropped": 0,
           "skipped_existing": 0, "drafts": [], "drops": [], "errors": [],
           "ran_at": datetime.utcnow().isoformat() + "Z"}
    c = _conn()
    if c is None:
        out["ok"] = False
        out["errors"].append("no_database")
        return out
    try:
        _ensure_queue(c)
        lost = _find_lost_queries(c, days=days, limit=limit)
        out["checked"] = len(lost)
        facts = _gather_verified_facts(c)  # verified once per run; reused per draft
        if not facts:
            out["errors"].append("no_verified_facts_available — nothing drafted")
            return out

        for item in lost:
            query = item["query"]
            if not query:
                continue
            # Skip queries already in the queue (any status) — idempotent reruns.
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM media_citation_content_queue "
                        "WHERE query = %s LIMIT 1", (query,))
                    if cur.fetchone():
                        out["skipped_existing"] += 1
                        continue
            except Exception:
                pass

            text, err = _draft_answer(query, facts)
            if err or not text:
                out["errors"].append({"query": query[:80], "err": err or "no_text"})
                continue

            ok, reason = _run_guards(c, text)
            if not ok:
                out["dropped"] += 1
                out["drops"].append({"query": query[:80], "reason": reason[:160]})
                # Record the drop (status='rejected') so the operator can see what
                # the guards caught — but it is NOT a draft and NEVER publishable.
                try:
                    with c.cursor() as cur:
                        cur.execute("""
                            INSERT INTO media_citation_content_queue
                                (query, competitors, draft_title, draft_body,
                                 verified_facts, status, reject_reason)
                            VALUES (%s,%s::jsonb,%s,%s,%s::jsonb,'rejected',%s) ON CONFLICT DO NOTHING
                        """, (query, json.dumps(item["competitors"]),
                              _draft_title(query), text,
                              json.dumps(facts, default=str), reason[:300]))
                except Exception as e:
                    out["errors"].append({"query": query[:80],
                                          "err": f"reject_insert:{str(e)[:60]}"})
                continue

            # Passed BOTH guards → queue as a draft for operator review.
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO media_citation_content_queue
                            (query, competitors, draft_title, draft_body,
                             verified_facts, status)
                        VALUES (%s,%s::jsonb,%s,%s,%s::jsonb,'draft') ON CONFLICT DO NOTHING
                        RETURNING id
                    """, (query, json.dumps(item["competitors"]),
                          _draft_title(query), text,
                          json.dumps(facts, default=str)))
                    row = cur.fetchone()
                out["queued"] += 1
                out["drafts"].append({"id": (row[0] if row else None),
                                      "query": query[:80],
                                      "competitors": item["competitors"]})
            except Exception as e:
                out["errors"].append({"query": query[:80],
                                      "err": f"draft_insert:{str(e)[:60]}"})
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# ── endpoints (all admin-gated; all dark when flag off) ───────────────────────
@media_citation_gap_bp.route("/api/v1/media/citation-gap/run", methods=["POST"])
def run_endpoint():
    if not _enabled():
        return _disabled_response()
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    try:
        days = max(1, min(90, int(request.args.get("days") or 30)))
    except (ValueError, TypeError):
        days = 30
    try:
        limit = max(1, min(50, int(request.args.get("limit") or 15)))
    except (ValueError, TypeError):
        limit = 15
    try:
        return jsonify(_run(days=days, limit=limit)), 200
    except Exception as e:
        logger.warning("[citation_gap] run crashed: %s", str(e)[:200])
        return jsonify(ok=False, error="run_failed", detail=str(e)[:200]), 500


@media_citation_gap_bp.route("/api/v1/media/citation-gap/queue", methods=["GET"])
def queue_endpoint():
    if not _enabled():
        return _disabled_response()
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    status = (request.args.get("status") or "").strip().lower()
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except (ValueError, TypeError):
        limit = 50
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    rows = []
    try:
        _ensure_queue(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status in ("draft", "approved", "rejected"):
                cur.execute("""
                    SELECT id, created_at, query, competitors, draft_title,
                           draft_body, status, reject_reason, approved_at, approved_by
                      FROM media_citation_content_queue
                     WHERE status = %s
                     ORDER BY created_at DESC LIMIT %s
                """, (status, limit))
            else:
                cur.execute("""
                    SELECT id, created_at, query, competitors, draft_title,
                           draft_body, status, reject_reason, approved_at, approved_by
                      FROM media_citation_content_queue
                     ORDER BY created_at DESC LIMIT %s
                """, (limit,))
            for r in cur.fetchall() or []:
                rows.append({
                    "id": r["id"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "query": r["query"],
                    "competitors": r["competitors"] or [],
                    "draft_title": r["draft_title"],
                    "draft_body": r["draft_body"],
                    "status": r["status"],
                    "reject_reason": r["reject_reason"],
                    "approved_at": r["approved_at"].isoformat() if r["approved_at"] else None,
                    "approved_by": r["approved_by"],
                })
    except Exception as e:
        return jsonify(error="query_failed", detail=str(e)[:200]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass
    return jsonify(ok=True, count=len(rows), queue=rows,
                   note="Drafts are operator-review only. Approving does NOT publish."), 200


@media_citation_gap_bp.route("/api/v1/media/citation-gap/approve/<int:item_id>",
                             methods=["POST"])
def approve_endpoint(item_id: int):
    if not _enabled():
        return _disabled_response()
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_queue(c)
        # Approve ONLY a current draft. Approving records the operator's decision;
        # it does NOT publish, send, or post anything anywhere.
        who = (request.headers.get("X-Admin-Key")
               or request.headers.get("X-Internal-Key") or "operator")
        who = ("operator" if not who else who[:6] + "…")  # don't echo the key
        with c.cursor() as cur:
            cur.execute("""
                UPDATE media_citation_content_queue
                   SET status = 'approved', approved_at = NOW(), approved_by = %s
                 WHERE id = %s AND status = 'draft'
                 RETURNING id, query
            """, (who, item_id))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False,
                           error="not_found_or_not_draft",
                           detail="No draft with that id (already approved/rejected?)."), 404
        return jsonify(ok=True, id=row[0], query=row[1], status="approved",
                       note=("Approved for the operator's records. This does NOT "
                             "publish a page or post anywhere — publishing remains "
                             "a separate human step.")), 200
    except Exception as e:
        return jsonify(ok=False, error="approve_failed", detail=str(e)[:200]), 500
    finally:
        try:
            c.close()
        except Exception:
            pass


# ── module-load diagnostic (no side effects; never raises) ────────────────────
def _smoke():
    try:
        state = "ENABLED" if _enabled() else "dark (default)"
        logger.info("[citation_gap] loaded — MEDIA_CITATION_GAP_ENABLED=%s", state)
    except Exception:
        pass


_smoke()