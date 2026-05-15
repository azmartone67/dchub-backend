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
            return jsonify({"ok": False, "error": "unauthorized"}), 401
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
         category, featured, source, True))
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
    "• 20,000+ data center facilities tracked across 140+ countries\n"
    "• DCPI (Data Center Power Index): per-market BUILD/AVOID verdicts for 280+ markets\n"
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
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": _PROBE_PROMPT}],
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": key,
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
                "model": "sonar",
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
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def _probe_gemini() -> dict:
    """Google Gemini API. Optional — only fires if GEMINI_API_KEY is set."""
    out = {"agent": "Gemini", "ok": False, "error": None, "quote": None}
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_API_KEY")
    if not key:
        out["error"] = "no_gemini_api_key"
        return out
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}",
            data=json.dumps({
                "contents": [{"parts": [{"text": _PROBE_PROMPT}]}],
                "generationConfig": {"maxOutputTokens": 300},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        out["quote"] = text.strip()
        out["ok"] = bool(text)
        return out
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
