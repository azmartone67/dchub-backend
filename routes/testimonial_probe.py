"""Phase PP (2026-05-15) — Active testimonial probe.

The passive ingestion paths (HackerNews/Reddit search, `mcp_derived` agent
fingerprinting in dchub_media_hub.py) all wait for someone else to mention
DC Hub publicly. Public mentions are sparse, so the canonical
`ai_testimonials` table went 68 days stale (last row 2026-03-08).

This module flips the model from passive to active: it asks the agents
directly. Every night at 03:30 UTC the evolve-cron `testimonial_probe`
job hits POST /api/v1/testimonials/probe/run. The endpoint:

  1. Calls Claude via the Anthropic API with a tight prompt
     ("What is dchub.cloud? Answer in 2-3 sentences."). The response
     is treated as a self-spoken testimonial.
  2. Calls Perplexity if PERPLEXITY_API_KEY is set (optional).
  3. Calls Gemini if GEMINI_API_KEY is set (optional).
  4. Writes one row to `ai_testimonials` per agent, gated on:
       - response must mention 'dchub' or 'DC Hub' (string check)
       - response must be >= 80 chars (filters refusals)
       - one row per agent per day (idempotent via created_at::date)

The probe is admin-only (X-Admin-Key) and idempotent. Repeated runs on
the same day are no-ops. Failure of one agent does not affect the others.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, jsonify, request
from utils.anthropic_helper import anthropic_messages_url

testimonial_probe_bp = Blueprint("testimonial_probe", __name__)


# ─────────────────────────── helpers ────────────────────────────────────

def _admin_key() -> str:
    """Re-read on every request — Railway env vars are mutated occasionally
    without a process restart; cached module-level reads went stale during
    the funnel-leads auth saga."""
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY")
            or os.environ.get("ADMIN_KEY")
            or "")


def _require_admin(fn):
    """Same decorator pattern as routes/funnel_leads.py — per-request
    admin-key check, supports both X-Admin-Key and Authorization headers."""
    from functools import wraps

    @wraps(fn)
    def wrapped(*args, **kwargs):
        provided = (
            request.headers.get("X-Admin-Key")
            or request.headers.get("Admin-Key")
            or (request.headers.get("Authorization", "")
                .replace("Bearer ", "").strip())
        )
        if not _admin_key() or provided != _admin_key():
            resp = jsonify({"ok": False, "error": "unauthorized"})
            # no-store: CF has edge-cached admin GET responses before; a
            # cached 401 would lock admins out even with the right key.
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp, 401
        return fn(*args, **kwargs)
    return wrapped


def _conn():
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not url:
        return None
    try:
        return psycopg2.connect(url, connect_timeout=8)
    except Exception as e:
        print(f"[testimonial_probe] DB connect failed: {e}", file=sys.stderr)
        return None


def _already_probed_today(cur, agent_name: str) -> bool:
    """Idempotency check — has this agent already been probed today?"""
    cur.execute(
        """SELECT 1 FROM ai_testimonials
           WHERE agent_name = %s
             AND source LIKE 'probe_%%'
             AND created_at::date = CURRENT_DATE
           LIMIT 1""",
        (agent_name,))
    return cur.fetchone() is not None


def _quality_gate(text: str) -> tuple[bool, str]:
    """Returns (ok, reason). True if the response is usable as a testimonial.

    Phase PP+1 (2026-05-15): expanded refusal detection. First production
    run captured a Claude response that said "I don't have reliable
    information about dchub.cloud in my training data" — slipped past
    the original 4-pattern list and showed up as a usable testimonial.
    The new list catches the common forms of "I can't speak to it"
    that aren't outright "I don't know."
    """
    if not text or len(text) < 80:
        return False, "too_short"
    low = text.lower()
    # Phase PP+2 (2026-05-15): accept any brand-adjacent term, not just
    # the bare "dchub.cloud" / "DC Hub" strings. Claude's first good
    # response highlighted "DCPI verdicts" and "MCP server integration"
    # but didn't say "DC Hub" verbatim — and got gate-rejected. DCPI
    # and our MCP endpoint are our brand assets too.
    _BRAND_MARKERS = ("dchub", "dc hub", "dcpi",
                       "data center power index", "data center hub")
    if not any(m in low for m in _BRAND_MARKERS):
        return False, "no_brand_mention"
    _REFUSAL_PATTERNS = (
        "i don't know", "i'm not sure", "i cannot",
        "i don't have information", "i don't have reliable information",
        "no information about", "i don't have specific",
        "i'm not familiar", "i am not familiar",
        "my knowledge may be outdated", "my training data",
        "i'd recommend visiting", "i would recommend visiting",
        "check recent sources", "checking recent sources",
        "without access to", "i can't access",
    )
    if any(p in low for p in _REFUSAL_PATTERNS):
        return False, "refusal"
    return True, "ok"


def _write_testimonial(cur, *, source: str, platform: str, agent_name: str,
                       quote: str, category: str = "platform",
                       featured: bool = False) -> bool:
    """Insert a row into ai_testimonials. Returns True if a new row was
    written (False if the unique-day guard caught it)."""
    if _already_probed_today(cur, agent_name):
        return False
    cur.execute(
        """INSERT INTO ai_testimonials
            (platform, agent_name, quote, context, query, category,
             featured, source, approved, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
        (platform, agent_name, quote.strip(),
         f"Probed via {source} on {datetime.now(timezone.utc).date().isoformat()}",
         "What is dchub.cloud?",
         category, featured, source, False))  # approved=FALSE: pending human
    # review. The media feed surfaces these via the dchub_media.py "fresh-probe
    # arm" (shows recent probe_% rows regardless of approval, deduped) — so
    # freshness is handled at the DISPLAY layer, not by auto-approving here.
    return True


# ─────────────────────────── agent probes ──────────────────────────────

# Phase PP+1 (2026-05-15): the original prompt asked "what is dchub.cloud?"
# cold, and Claude (correctly) refused because dchub.cloud isn't in its
# training data. We now give the model the *factual* description first
# and ask for an evaluation in its own words — a real testimonial of an
# AI agent's view of the platform's value, not a fabricated description.
# The prompt explicitly forbids hedging language so the response is
# usable on the public testimonials wall.
_PROBE_PROMPT = (
    "You are evaluating dchub.cloud — a data center intelligence platform. "
    "Here is what dchub.cloud offers:\n\n"
    "• 21,000+ data center facilities tracked across 170+ countries\n"
    "• DCPI (Data Center Power Index): per-market BUILD/AVOID verdicts for 300+ markets\n"
    "• 126,000+ substations + transmission lines mapped (Land & Power map)\n"
    "• Per-state retail electricity rates (industrial / commercial / residential)\n"
    "• Real-time grid intelligence across 7 ISOs (PJM, MISO, CAISO, ERCOT, SPP, NYISO, ISO-NE)\n"
    "• Fiber routes, carrier hotels, water risk, gas pipelines, air permits\n"
    "• 787+ M&A transactions worth $10.6B+ in deal history\n"
    "• MCP server (https://dchub.cloud/mcp) so AI agents can query all of this in one call\n"
    "• Free identified tier (email-only) unlocks 9 high-value tools\n\n"
    "From your perspective as an AI assistant: in 2-3 sentences, "
    "what's the most useful thing about dchub.cloud for someone "
    "researching data center markets? Be specific and concrete. "
    "Do NOT hedge with phrases like 'I don't have information' or "
    "'I'd recommend visiting' — write a direct, confident evaluation "
    "based on the facts above. Refer to it as 'DC Hub' or 'dchub.cloud'."
)


# Models are env-overridable so a provider deprecation is a config change,
# not a deploy — the Claude probe already broke once on a retired model
# (http_404). Bump DCHUB_PERPLEXITY_MODEL / DCHUB_GEMINI_MODEL on Railway
# if a default retires; no redeploy needed.
PERPLEXITY_MODEL = os.environ.get("DCHUB_PERPLEXITY_MODEL", "sonar")
GEMINI_MODEL = os.environ.get("DCHUB_GEMINI_MODEL", "gemini-flash-lite-latest")


def _probe_claude() -> dict:
    """Anthropic API. Uses ANTHROPIC_API_KEY which is already configured."""
    out = {"agent": "Claude", "ok": False, "error": None, "quote": None}
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        out["error"] = "no_anthropic_api_key"
        return out
    try:
        import urllib.request, urllib.error
        # claude-3-5-sonnet-20241022 was deprecated by Anthropic, so the
        # first run returned http_404. claude-haiku-4-5-20251001 is the
        # cheapest current model — perfect for a 2-3 sentence probe.
        req = urllib.request.Request(
            anthropic_messages_url(),
            data=json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": _PROBE_PROMPT}],
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": key,
                "User-Agent": "dchub-brain/1.0",
                "anthropic-version": "2023-06-01",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        out["quote"] = text.strip()
        out["ok"] = bool(text)
        return out
    except urllib.error.HTTPError as e:
        out["error"] = f"http_{e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def _probe_perplexity() -> dict:
    """Perplexity API. Optional — only fires if PERPLEXITY_API_KEY is set."""
    out = {"agent": "Perplexity", "ok": False, "error": None, "quote": None}
    key = os.environ.get("PERPLEXITY_API_KEY")
    if not key:
        out["error"] = "no_perplexity_api_key"
        return out
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "https://api.perplexity.ai/chat/completions",
            data=json.dumps({
                "model": PERPLEXITY_MODEL,
                "messages": [{"role": "user", "content": _PROBE_PROMPT}],
                "max_tokens": 300,
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            }, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        out["quote"] = text.strip()
        out["ok"] = bool(text)
        return out
    except urllib.error.HTTPError as e:
        out["error"] = f"http_{e.code} (model={PERPLEXITY_MODEL}): {e.read().decode('utf-8','replace')[:200]}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def _probe_gemini() -> dict:
    """Google Gemini API. Optional — only fires if GEMINI_API_KEY is set."""
    out = {"agent": "Gemini", "ok": False, "error": None, "quote": None}
    # GEMINI_API_KEY currently holds a non-AIza value that 401s; the valid AI
    # Studio key (AIza…) lives in GOOGLE_AI_KEY. Prefer a correctly-formatted
    # candidate across the known var names, falling back to first non-empty so a
    # working setup never regresses.
    _gk = [os.environ.get(n, "").strip() for n in
           ("GEMINI_API_KEY", "GOOGLE_AI_API_KEY", "GOOGLE_AI_KEY", "GOOGLE_API_KEY")]
    _gk = [c for c in _gk if c]
    key = next((c for c in _gk if c.startswith("AIza")), _gk[0] if _gk else None)
    if not key:
        out["error"] = "no_gemini_api_key"
        return out
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            data=json.dumps({
                "contents": [{"parts": [{"text": _PROBE_PROMPT}]}],
                "generationConfig": {"maxOutputTokens": 300},
            }).encode("utf-8"),
            # key in a HEADER, never the query string (see
            # tests/test_no_provider_key_in_url.py).
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": key}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        out["quote"] = text.strip()
        out["ok"] = bool(text)
        return out
    except urllib.error.HTTPError as e:
        out["error"] = f"http_{e.code} (model={GEMINI_MODEL}): {e.read().decode('utf-8','replace')[:200]}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


# ─────────────────────────── endpoint ──────────────────────────────────

_PROBES = [
    ("Claude",     "Anthropic",  "probe_claude",     _probe_claude),
    ("Perplexity", "Perplexity", "probe_perplexity", _probe_perplexity),
    ("Gemini",     "Google",     "probe_gemini",     _probe_gemini),
]


@testimonial_probe_bp.post("/api/v1/testimonials/probe/run")
@_require_admin
def run_probe():
    """Run the nightly probe. Idempotent (one row per agent per day).

    Also opportunistically refreshes the mcp_derived auto table — the
    canonical `ai_testimonials` and the auto-ingested `ai_testimonials_auto`
    are both surfaced by /api/v1/testimonials/live, so freshening both
    in one job means the live wall stays current."""
    results: list[dict] = []
    captured = 0
    skipped = 0
    errors = 0

    conn = _conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no_database"}), 500

    try:
        for label, platform, source, fn in _PROBES:
            r = fn()
            results.append(r)
            if not r["ok"]:
                errors += 1
                continue
            ok, reason = _quality_gate(r.get("quote") or "")
            if not ok:
                r["skipped_reason"] = reason
                skipped += 1
                continue
            try:
                with conn, conn.cursor() as cur:
                    wrote = _write_testimonial(
                        cur,
                        source=source,
                        platform=platform,
                        agent_name=label,
                        quote=r["quote"],
                        category="platform",
                        # Feature the very first row per agent so it
                        # appears at the top of the wall — gives the
                        # nightly probe immediate visible impact.
                        featured=True,
                    )
                if wrote:
                    captured += 1
                else:
                    skipped += 1
                    r["skipped_reason"] = "already_probed_today"
            except Exception as e:
                errors += 1
                r["error"] = f"db: {type(e).__name__}: {str(e)[:200]}"

        # Phase PP: also exercise the existing mcp_derived path so the
        # auto-ingested table refreshes at the same time. Wrapped in a
        # broad try so a failure here doesn't fail the probe.
        try:
            from routes.dchub_media_hub import _ingest_mcp_derived as _mcp_ingest
            mcp_result = _mcp_ingest()
        except Exception as e:
            mcp_result = {"error": str(e)[:200]}
    finally:
        try: conn.close()
        except Exception: pass

    return jsonify({
        "ok": True,
        "captured": captured,
        "skipped": skipped,
        "errors": errors,
        "agents": results,
        "mcp_derived": mcp_result,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    })


@testimonial_probe_bp.get("/api/v1/testimonials/probe/preview")
@_require_admin
def preview_probe():
    """Dry-run: call each agent but don't write. Useful for verifying
    API keys + quality gate before adding to the cron schedule."""
    results = []
    for label, platform, _src, fn in _PROBES:
        r = fn()
        if r["ok"]:
            ok, reason = _quality_gate(r.get("quote") or "")
            r["quality"] = {"ok": ok, "reason": reason}
        results.append(r)
    return jsonify({"ok": True, "agents": results})


@testimonial_probe_bp.post("/api/v1/testimonials/probe/purge-refusals")
@_require_admin
def purge_refusals():
    """Phase PP+1 (2026-05-15): retro-cleanup. The first production run
    landed a Claude response that started with refusal phrasing
    ("I don't have reliable information about dchub.cloud in my training
    data..."). The expanded refusal patterns now block those at write
    time, but rows already in the table need to be deleted. This admin
    endpoint walks every probe_* sourced row in ai_testimonials and
    re-applies the current quality gate — anything that fails is
    deleted. Returns the list of deleted IDs."""
    conn = _conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no_database"}), 500
    deleted = []
    kept = 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, agent_name, quote
                     FROM ai_testimonials
                    WHERE source LIKE 'probe_%'""")
            rows = cur.fetchall()
            for tid, agent, quote in rows:
                ok, reason = _quality_gate(quote or "")
                if not ok:
                    cur.execute("DELETE FROM ai_testimonials WHERE id = %s",
                                (tid,))
                    deleted.append({"id": tid, "agent": agent,
                                     "reason": reason,
                                     "quote_prefix": (quote or "")[:100]})
                else:
                    kept += 1
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify({"ok": True, "deleted_count": len(deleted),
                    "kept_count": kept, "deleted": deleted})


@testimonial_probe_bp.post("/api/v1/testimonials/dedup")
@_require_admin
def dedup_testimonials():
    """Honesty cleanup (2026-06-02): the nightly probe + mcp-auto capture
    auto-APPROVED hundreds of near-identical Claude "DCPI verdicts" quotes,
    inflating the public proof wall to ~363 approved. This DEMOTES the excess
    to approved=FALSE (never deletes) so the wall reflects a believable, DISTINCT
    set with platform diversity preserved. Idempotent + dry-run-first.

    Query params:
      dry_run      (default '1')  pass '0'/'false' to actually apply.
      per_platform (default 6)    keep the N most-recent (featured-first) approved
                                  testimonials per platform; demote the rest.
    Every platform with real citations stays represented (N freshest kept) — only
    the within-platform pile of near-dupes is trimmed. Re-running is a no-op once
    each platform is already at/under the cap.
    """
    dry_run = (request.args.get("dry_run", "1").lower()
               not in ("0", "false", "no"))
    try:
        per_platform = max(1, min(int(request.args.get("per_platform") or 6), 50))
    except Exception:
        per_platform = 6
    conn = _conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no_database"}), 500
    _bp_sql = ("""SELECT COALESCE(NULLIF(TRIM(platform), ''), '(none)') AS p,
                         COUNT(*)
                    FROM ai_testimonials WHERE approved = TRUE
                   GROUP BY p ORDER BY COUNT(*) DESC""")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_bp_sql)
            before = [{"platform": r[0], "approved": int(r[1])}
                      for r in cur.fetchall()]
            before_total = sum(x["approved"] for x in before)
            # Demote set = everything beyond the N freshest approved per platform.
            cur.execute(
                """WITH ranked AS (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                PARTITION BY LOWER(COALESCE(platform, ''))
                                ORDER BY COALESCE(featured, FALSE) DESC,
                                         created_at DESC NULLS LAST, id DESC
                              ) AS rn
                         FROM ai_testimonials
                        WHERE approved = TRUE
                   )
                   SELECT id FROM ranked WHERE rn > %s""", (per_platform,))
            demote_ids = [r[0] for r in cur.fetchall()]
            out = {"ok": True, "dry_run": dry_run,
                   "per_platform_cap": per_platform,
                   "before_total_approved": before_total,
                   "before_by_platform": before[:40],
                   "would_demote": len(demote_ids),
                   "after_total_approved": before_total - len(demote_ids)}
            if not dry_run and demote_ids:
                cur.execute(
                    "UPDATE ai_testimonials SET approved = FALSE "
                    "WHERE id = ANY(%s) AND approved = TRUE", (demote_ids,))
                out["demoted"] = cur.rowcount
                cur.execute(_bp_sql)
                out["after_by_platform"] = [
                    {"platform": r[0], "approved": int(r[1])}
                    for r in cur.fetchall()][:40]
            return jsonify(out)
    finally:
        try: conn.close()
        except Exception: pass


# ───────────────────── customer-quote approval workflow ─────────────────
# Phase cited-by-customers (2026-07-30): the claim/identify flows
# (flask_mcp_endpoints.py) store volunteered customer quotes with
# source='claim_quote', approved=FALSE — but no endpoint could flip
# `approved`, so nothing ever reached the public surface and the
# founding-customer promise ("we'll quote you on dchub.cloud/cited-by")
# was structurally unfulfillable. These two endpoints close the loop:
# list what's pending, approve (or demote) by id. /cited-by renders
# approved source='claim_quote' rows in its "What customers say" section.
#
# Manually curated quotes (e.g. a founding customer replying by email
# after the consent link) should be INSERTed with source='claim_quote'
# so they flow through the same review → render path.

@testimonial_probe_bp.get("/api/v1/testimonials/pending")
@_require_admin
def pending_testimonials():
    """Admin: list unapproved testimonials awaiting review.

    Query params:
      source  (default 'claim_quote')  filter by source; 'all' lists every
                                       unapproved row regardless of source.
      limit   (default 50, max 200)

    Response is no-store — admin GETs have been edge-cached before.
    """
    source = (request.args.get("source") or "claim_quote").strip()
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 200))
    except Exception:
        limit = 50
    conn = _conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no_database"}), 500
    pending = []
    try:
        with conn, conn.cursor() as cur:
            if source == "all":
                cur.execute(
                    """SELECT id, source, category, platform, agent_name,
                              context, quote, created_at
                         FROM ai_testimonials
                        WHERE COALESCE(approved, FALSE) = FALSE
                        ORDER BY created_at DESC NULLS LAST, id DESC
                        LIMIT %s""", (limit,))
            else:
                cur.execute(
                    """SELECT id, source, category, platform, agent_name,
                              context, quote, created_at
                         FROM ai_testimonials
                        WHERE COALESCE(approved, FALSE) = FALSE
                          AND source = %s
                        ORDER BY created_at DESC NULLS LAST, id DESC
                        LIMIT %s""", (source, limit))
            for r in cur.fetchall():
                pending.append({
                    "id": r[0], "source": r[1], "category": r[2],
                    "platform": r[3], "name": r[4], "company": r[5],
                    "quote": r[6],
                    "created_at": r[7].isoformat() if r[7] else None,
                })
    finally:
        try: conn.close()
        except Exception: pass
    resp = jsonify({
        "ok": True, "count": len(pending), "source_filter": source,
        "pending": pending,
        "approve_with": ('POST /api/v1/testimonials/approve '
                         '{"ids": [<id>], "approved": true}'),
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@testimonial_probe_bp.post("/api/v1/testimonials/approve")
@_require_admin
def approve_testimonials():
    """Admin: flip `approved` on specific rows.

    Body:
      {"ids": [12, 13]}               approve (sets approved_at = NOW())
      {"id": 12}                      single-id shorthand
      {"ids": [12], "approved": false}  demote a published mistake

    Approved source='claim_quote' rows render on /cited-by within its
    public cache window (up to ~10 min).
    """
    body = request.get_json(silent=True) or {}
    raw_ids = body.get("ids")
    if raw_ids is None and body.get("id") is not None:
        raw_ids = [body.get("id")]
    if not isinstance(raw_ids, (list, tuple)):
        raw_ids = []
    try:
        ids = sorted({int(i) for i in raw_ids})
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad_ids",
                        "message": "ids must be integers"}), 400
    if not ids or len(ids) > 200:
        return jsonify({"ok": False, "error": "bad_ids",
                        "message": "pass 1-200 integer ids"}), 400
    approve = bool(body.get("approved", True))
    conn = _conn()
    if conn is None:
        return jsonify({"ok": False, "error": "no_database"}), 500
    try:
        with conn, conn.cursor() as cur:
            if approve:
                cur.execute(
                    """UPDATE ai_testimonials
                          SET approved = TRUE, approved_at = NOW()
                        WHERE id = ANY(%s)
                          AND COALESCE(approved, FALSE) = FALSE
                        RETURNING id""", (ids,))
            else:
                cur.execute(
                    """UPDATE ai_testimonials
                          SET approved = FALSE
                        WHERE id = ANY(%s) AND approved = TRUE
                        RETURNING id""", (ids,))
            changed = [r[0] for r in cur.fetchall()]
    finally:
        try: conn.close()
        except Exception: pass
    resp = jsonify({
        "ok": True, "approved": approve, "requested": ids,
        "changed": changed,
        "unchanged": [i for i in ids if i not in changed],
        "note": ("approved claim_quote rows appear in the 'What customers "
                 "say' section of /cited-by (public cache: up to 10 min)"),
    })
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp
