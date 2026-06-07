"""routes/ai_platform_onboarder.py — AI Agent Expansion Round 1, Unlock 1 (2026-06-07).

Theme: get MORE AI platforms using DC Hub as their data layer.
Today 14 platforms are connected (Claude, ChatGPT, Meta AI, Gemini, Copilot,
Perplexity, Cursor, Cline, Continue, Codeium, Sourcegraph, Tabnine, You.com,
Phind). Round 1 = unblock the next 20.

This module processes the "Submit my platform" queue:
  1. New rows land in `ai_platform_submissions` from /ai-agents form, MCP
     server `submit_platform` tool, or POST /api/v1/platforms/register
     (Unlock 3).
  2. This cron (every 6h) reads queue, enriches each submission:
       - Verify URL is reachable (HEAD with 4s timeout)
       - Scrape <title>, <meta name="description">, contact links
       - Compute fit_score (1-100) — MCP-capable, data-intensive, B2B
       - For fit ≥70: brain Claude-writes a 200-word integration card
       - For fit ≥85: status='auto_approved' (added to connected list)
       - For fit 70-84: status='pending_review' (human queue)
       - For fit <70: status='rejected_low_fit' (still in queue for audit)
       - Resend email to contact_email confirming submission outcome.

Endpoints:
  POST /api/v1/admin/platforms/process       — run the queue (cron/internal)
  GET  /api/v1/admin/platforms/queue         — see pending+recent
  POST /api/v1/admin/platforms/approve/<id>  — manual approve
  POST /api/v1/admin/platforms/reject/<id>   — ?reason=<x>
  GET  /api/v1/platforms/connected           — public, lists connected_ai_platforms
  GET  /admin/ai-platforms                   — admin dashboard HTML

Safety:
  * Kill switch: AI_AGENT_EXPANSION_DISABLE=1
  * Auto-approval gated to fit_score ≥85 (high bar)
  * Per-submission Claude call max once (status flag prevents re-enrich)
  * Email send dedup via metadata.confirmation_sent_at
  * Defensive timeouts: 4s HEAD, 6s GET, 40s Claude
  * Idempotent: re-running the cron is safe (rows skipped by status).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from internal_auth import accepted_internal_keys


logger = logging.getLogger(__name__)
ai_platform_onboarder_bp = Blueprint("ai_platform_onboarder", __name__)


# ── Auth ─────────────────────────────────────────────────────────────
_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _disabled() -> bool:
    return os.environ.get("AI_AGENT_EXPANSION_DISABLE", "").strip() == "1"


# ── Config ───────────────────────────────────────────────────────────
AUTO_APPROVE_MIN = int(os.environ.get("AI_PLATFORM_AUTO_APPROVE_MIN", "85"))
ENRICH_MIN       = int(os.environ.get("AI_PLATFORM_ENRICH_MIN",       "70"))
PER_RUN_CAP      = int(os.environ.get("AI_PLATFORM_PER_RUN_CAP",      "20"))


# ── DB ───────────────────────────────────────────────────────────────
def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ── HTTP helpers ─────────────────────────────────────────────────────
_UA = "DCHub-PlatformOnboarder/1.0 (+https://dchub.cloud)"


def _url_reachable(url: str) -> tuple[bool, int]:
    """HEAD then GET fallback. Returns (ok, status_code)."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=4) as r:
            return (200 <= r.status < 400, r.status)
    except urllib.error.HTTPError as e:
        # 405 = HEAD not allowed → try GET
        if e.code == 405:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": _UA})
                with urllib.request.urlopen(req, timeout=6) as r:
                    return (200 <= r.status < 400, r.status)
            except Exception:
                return (False, e.code)
        return (False, e.code)
    except Exception:
        return (False, 0)


_TITLE_RE = re.compile(r"<title[^>]*>([^<]{1,200})</title>", re.I)
_DESC_RE  = re.compile(
    r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']{1,400})["\']', re.I)
_CONTACT_RE = re.compile(r'mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', re.I)


def _scrape_meta(url: str) -> dict:
    """Best-effort fetch of title/description/contact_email. Always returns
    dict (empty on failure). 6s timeout."""
    out = {"title": "", "description": "", "scraped_email": "", "status": 0,
           "mcp_hint": False, "api_hint": False, "b2b_hint": False}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=6) as r:
            out["status"] = r.status
            body = r.read(80000).decode("utf-8", errors="ignore")
        t = _TITLE_RE.search(body)
        if t: out["title"] = re.sub(r"\s+", " ", t.group(1)).strip()[:200]
        d = _DESC_RE.search(body)
        if d: out["description"] = re.sub(r"\s+", " ", d.group(1)).strip()[:400]
        m = _CONTACT_RE.search(body)
        if m: out["scraped_email"] = m.group(1)[:120]
        low = body.lower()
        out["mcp_hint"] = any(kw in low for kw in
                              ("mcp", "model context protocol", "/mcp", "anthropic"))
        out["api_hint"] = any(kw in low for kw in
                              ("/api/", "developer", "rest api", "graphql", "sdk"))
        out["b2b_hint"] = any(kw in low for kw in
                              ("enterprise", "for teams", "pricing",
                               "platform", "saas", "b2b"))
    except Exception:
        pass
    return out


# ── Fit score ────────────────────────────────────────────────────────
def _compute_fit_score(submission: dict, meta: dict, reachable: bool) -> tuple[int, list]:
    """Return (score, reasons[]). Score 1-100. Composition:
       reachable URL      → +20
       MCP-capable signal → +30 (biggest)
       Data-intensive     → +20 (API/SDK hints, type='mcp' or 'api')
       B2B signal         → +15
       Contact email      → +10
       Description filled → +5
    """
    s = 0
    reasons = []
    if reachable:
        s += 20; reasons.append("reachable URL")
    if meta.get("mcp_hint") or (submission.get("type", "").lower() == "mcp"):
        s += 30; reasons.append("MCP-capable")
    if meta.get("api_hint") or (submission.get("type", "").lower() in ("api", "mcp")):
        s += 20; reasons.append("data-intensive (API/SDK)")
    if meta.get("b2b_hint"):
        s += 15; reasons.append("B2B platform signals")
    if submission.get("contact_email") or meta.get("scraped_email"):
        s += 10; reasons.append("contact email present")
    desc = (submission.get("description") or meta.get("description") or "").strip()
    if len(desc) >= 40:
        s += 5; reasons.append("non-trivial description")
    s = max(0, min(100, s))
    return s, reasons


# ── Claude integration card writer ───────────────────────────────────
def _claude_write_card(submission: dict, meta: dict, fit_score: int) -> str | None:
    """Generate a 200-word integration card via Claude. Returns None on failure."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        from utils.anthropic_helper import anthropic_messages_url
        url = anthropic_messages_url()
    except Exception:
        url = "https://api.anthropic.com/v1/messages"
    try:
        from routes.brain_models import resolve_chain
        models = resolve_chain(os.environ.get("DCHUB_BRAIN_MODEL_ROUTINE") or
                               os.environ.get("DCHUB_BRAIN_MODEL") or
                               "claude-sonnet-4-20250514")
    except Exception:
        models = ["claude-sonnet-4-20250514"]
    prompt = (
        "Write a 200-word integration card for an AI platform that "
        "wants to use DC Hub (the data-center intelligence platform) as "
        "its data layer. Match the tone of an operator brief: clear, "
        "factual, action-oriented. Cover (1) what this platform does, "
        "(2) what DC Hub data they'd query, (3) example MCP tool calls "
        "they'd use, (4) the integration shape (MCP vs REST). Do NOT add "
        "marketing fluff or speculation.\n\n"
        f"Platform name: {submission.get('name', '?')}\n"
        f"URL: {submission.get('url', '?')}\n"
        f"Type: {submission.get('type', 'unknown')}\n"
        f"Self-described: {(submission.get('description') or '')[:400]}\n"
        f"Scraped title: {meta.get('title', '')}\n"
        f"Scraped description: {meta.get('description', '')}\n"
        f"Fit score: {fit_score}/100 "
        f"(MCP={meta.get('mcp_hint')}, API={meta.get('api_hint')}, "
        f"B2B={meta.get('b2b_hint')})\n"
    )
    sysmsg = (
        "You write integration cards for AI platforms joining DC Hub. "
        "200 words max, plain text, no markdown headers. Lead with one "
        "sentence on what the platform does, then dive into the data fit."
    )
    for i, model in enumerate(models[:3]):
        try:
            body = json.dumps({
                "model": model,
                "max_tokens": 700,
                "system": sysmsg,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": "dchub-brain/1.0",
                "Anthropic-Version": "2023-06-01",
            })
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
            for block in (data.get("content") or []):
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        return text[:2000]
            return None
        except urllib.error.HTTPError as e:
            if e.code in (404, 400) and i + 1 < len(models):
                continue
            return None
        except Exception:
            return None
    return None


# ── Email confirmation via Resend ────────────────────────────────────
def _send_confirmation(email: str, name: str, status: str, fit_score: int,
                       card_excerpt: str = "") -> bool:
    """Send a confirmation email via Resend. Returns True on success."""
    resend_key = (os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
    if not resend_key or not email or "@" not in email:
        return False
    sender = os.environ.get("DCHUB_RESEND_FROM", "DC Hub <noreply@dchub.cloud>")
    status_copy = {
        "auto_approved": (
            "Great news — your platform is in.",
            f"Your platform <strong>{name}</strong> scored {fit_score}/100 "
            "and is now listed at https://dchub.cloud/ai-agents."),
        "pending_review": (
            "Got it — your platform is in our review queue.",
            f"<strong>{name}</strong> scored {fit_score}/100. Our team reviews "
            "this within 48h; you'll hear back."),
        "rejected_low_fit": (
            "Thanks for the submission.",
            f"<strong>{name}</strong> scored {fit_score}/100 against our fit "
            "rubric (MCP-capable, data-intensive, B2B). We're keeping the "
            "submission on file."),
    }.get(status, ("Submission received", f"<strong>{name}</strong> is in the queue."))
    excerpt_html = ""
    if card_excerpt:
        from html import escape as _esc
        excerpt_html = (
            f'<div style="background:#f7f8fa;border-radius:6px;padding:14px;'
            f'font-size:13px;color:#444;line-height:1.55;margin:18px 0">'
            f'<div style="font-size:11px;color:#888;letter-spacing:.05em;'
            f'text-transform:uppercase;margin-bottom:6px">Integration card '
            f'(preview)</div>{_esc(card_excerpt[:400])}…</div>')
    html = f"""<!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:540px;margin:0 auto;padding:28px;color:#1a1a1a">
<div style="font-size:11px;color:#888;letter-spacing:.05em;text-transform:uppercase;margin-bottom:10px">DC Hub &middot; platform onboarding</div>
<h2 style="margin:0 0 12px;font-size:22px">{status_copy[0]}</h2>
<p style="color:#555;font-size:15px;line-height:1.55">{status_copy[1]}</p>
{excerpt_html}
<p style="margin:22px 0"><a href="https://dchub.cloud/ai-agents" style="background:#1976d2;color:#fff;padding:11px 22px;border-radius:6px;text-decoration:none;font-weight:600;display:inline-block">See connected platforms &rarr;</a></p>
<hr style="border:0;border-top:1px solid #eee;margin:28px 0">
<p style="font-size:12px;color:#888">Submitted via dchub.cloud/ai-agents. Reply to this email if anything looks off.</p>
</body></html>"""
    try:
        import requests as _rq
        resp = _rq.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}",
                     "Content-Type": "application/json"},
            json={"from": sender, "to": [email],
                  "subject": f"DC Hub submission · {name}",
                  "html": html},
            timeout=12)
        return resp.status_code in (200, 201)
    except Exception:
        return False


# ── Connected list helper ────────────────────────────────────────────
def _ensure_connected(c, submission: dict, fit_score: int, card: str, status: str) -> None:
    """Insert into connected_ai_platforms (idempotent UPSERT). Adds the
    platform to the public /ai-agents list. Table is auto-created in the
    schema_repair sweep, but we use IF NOT EXISTS to be self-healing."""
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS connected_ai_platforms (
                    id            SERIAL PRIMARY KEY,
                    name          TEXT NOT NULL,
                    url           TEXT,
                    type          TEXT,
                    integration_card TEXT,
                    fit_score     INTEGER,
                    status        TEXT NOT NULL DEFAULT 'auto_approved',
                    source        TEXT,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(name)
                )""")
            cur.execute("""
                INSERT INTO connected_ai_platforms
                    (name, url, type, integration_card, fit_score, status, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                   SET url = EXCLUDED.url,
                       integration_card = COALESCE(EXCLUDED.integration_card,
                                                   connected_ai_platforms.integration_card),
                       fit_score = EXCLUDED.fit_score,
                       status = EXCLUDED.status
            """, (submission["name"], submission.get("url"),
                  submission.get("type"), card, fit_score, status,
                  "auto_onboarder"))
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning("connected upsert failed: %s", e)
        try: c.rollback()
        except Exception: pass


# ── Process queue (the main loop) ────────────────────────────────────
def _process_one(c, row: dict) -> dict:
    """Process a single pending submission. Returns result dict."""
    sub_id = row["id"]
    url = (row.get("url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    reachable, code = _url_reachable(url) if url else (False, 0)
    meta = _scrape_meta(url) if reachable else {}
    submission = {**row, "url": url}
    fit_score, reasons = _compute_fit_score(submission, meta, reachable)

    card = ""
    if fit_score >= ENRICH_MIN:
        card = _claude_write_card(submission, meta, fit_score) or ""

    if fit_score >= AUTO_APPROVE_MIN:
        status = "auto_approved"
    elif fit_score >= ENRICH_MIN:
        status = "pending_review"
    else:
        status = "rejected_low_fit"

    # Persist enrichment back to submissions row.
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE ai_platform_submissions
                   SET fit_score = %s,
                       status = %s,
                       integration_card = %s,
                       enrichment_meta = %s,
                       processed_at = NOW(),
                       approved_at = CASE WHEN %s = 'auto_approved'
                                          THEN NOW() ELSE approved_at END,
                       approved_by = CASE WHEN %s = 'auto_approved'
                                          THEN 'auto_onboarder' ELSE approved_by END
                 WHERE id = %s
            """, (fit_score, status, card,
                  json.dumps({"reachable": reachable, "http_code": code,
                              "meta": meta, "reasons": reasons})[:4000],
                  status, status, sub_id))
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning("submission %s update failed: %s", sub_id, e)
        try: c.rollback()
        except Exception: pass

    # Promote auto-approved to connected list.
    if status == "auto_approved":
        _ensure_connected(c, submission, fit_score, card, status)

    # Send confirmation email (dedup checked via column).
    email = row.get("contact_email") or meta.get("scraped_email") or ""
    sent = False
    if email and not row.get("confirmation_sent_at"):
        sent = _send_confirmation(email, row.get("name", "your platform"),
                                  status, fit_score, card)
        if sent:
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE ai_platform_submissions SET confirmation_sent_at = NOW() WHERE id = %s",
                        (sub_id,))
                try: c.commit()
                except Exception: pass
            except Exception:
                pass

    return {"id": sub_id, "name": row.get("name"), "fit_score": fit_score,
            "status": status, "reachable": reachable, "card_written": bool(card),
            "confirmation_sent": sent, "reasons": reasons}


def _fetch_pending(c, limit: int) -> list[dict]:
    rows = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, name, url, type, contact_email, description,
                       fit_score, status, confirmation_sent_at, submitted_at
                  FROM ai_platform_submissions
                 WHERE status IN ('pending', 'new')
                    OR (status IS NULL)
                 ORDER BY submitted_at NULLS LAST, id
                 LIMIT %s
            """, (limit,))
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                rows.append(dict(zip(cols, r)))
    except Exception as e:
        logger.warning("fetch_pending failed: %s", e)
        try: c.rollback()
        except Exception: pass
    return rows


# ── Endpoints ────────────────────────────────────────────────────────
@ai_platform_onboarder_bp.route("/api/v1/admin/platforms/process", methods=["POST", "GET"])
def process_queue():
    """Cron entry — process up to PER_RUN_CAP pending submissions."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if _disabled():
        return jsonify(ok=True, skipped=True, reason="AI_AGENT_EXPANSION_DISABLE=1"), 200
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    started = time.time()
    pending = _fetch_pending(c, PER_RUN_CAP)
    results = []
    for row in pending:
        try:
            results.append(_process_one(c, row))
        except Exception as e:
            logger.error("process_one(%s) raised: %s", row.get("id"), e)
            results.append({"id": row.get("id"), "error": repr(e)[:200]})
    return jsonify(
        ok=True,
        processed=len(results),
        auto_approved=sum(1 for r in results if r.get("status") == "auto_approved"),
        pending_review=sum(1 for r in results if r.get("status") == "pending_review"),
        rejected=sum(1 for r in results if r.get("status") == "rejected_low_fit"),
        elapsed_s=round(time.time() - started, 2),
        results=results,
    )


@ai_platform_onboarder_bp.route("/api/v1/admin/platforms/queue", methods=["GET"])
def queue_view():
    """List pending + recent processed submissions for the admin dashboard."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    out = {"pending": [], "recent": []}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, name, url, type, contact_email, fit_score, status,
                       submitted_at, processed_at, approved_at, approved_by
                  FROM ai_platform_submissions
                 WHERE status IN ('pending', 'new') OR status IS NULL
                 ORDER BY submitted_at NULLS LAST, id
                 LIMIT 100
            """)
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                for k, v in list(d.items()):
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                out["pending"].append(d)
            cur.execute("""
                SELECT id, name, url, type, fit_score, status, processed_at, approved_by
                  FROM ai_platform_submissions
                 WHERE status NOT IN ('pending', 'new') AND status IS NOT NULL
                 ORDER BY processed_at DESC NULLS LAST
                 LIMIT 50
            """)
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                for k, v in list(d.items()):
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                out["recent"].append(d)
    except Exception as e:
        logger.warning("queue_view failed: %s", e)
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error="db_error", detail=repr(e)[:200]), 500
    out["ok"] = True
    out["counts"] = {"pending": len(out["pending"]), "recent": len(out["recent"])}
    return jsonify(out)


@ai_platform_onboarder_bp.route("/api/v1/admin/platforms/approve/<int:sub_id>",
                                methods=["POST"])
def approve(sub_id: int):
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE ai_platform_submissions
                   SET status = 'approved',
                       approved_at = NOW(),
                       approved_by = COALESCE(approved_by, 'manual')
                 WHERE id = %s
             RETURNING name, url, type, integration_card, fit_score
            """, (sub_id,))
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        name, url, ptype, card, fit = row
        _ensure_connected(c, {"name": name, "url": url, "type": ptype},
                          int(fit or 70), card or "", "approved")
        return jsonify(ok=True, id=sub_id, name=name, promoted=True)
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error="db_error", detail=repr(e)[:200]), 500


@ai_platform_onboarder_bp.route("/api/v1/admin/platforms/reject/<int:sub_id>",
                                methods=["POST"])
def reject(sub_id: int):
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    reason = (request.args.get("reason") or
              (request.json or {}).get("reason") or "manual_reject")[:200]
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE ai_platform_submissions
                   SET status = 'rejected',
                       approved_at = NOW(),
                       approved_by = 'manual',
                       enrichment_meta = COALESCE(enrichment_meta, '{}'::jsonb)
                                       || jsonb_build_object('reject_reason', %s::text)
                 WHERE id = %s
             RETURNING name
            """, (reason, sub_id))
            row = cur.fetchone()
        try: c.commit()
        except Exception: pass
        if not row:
            return jsonify(ok=False, error="not_found"), 404
        return jsonify(ok=True, id=sub_id, name=row[0], reason=reason)
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error="db_error", detail=repr(e)[:200]), 500


@ai_platform_onboarder_bp.route("/api/v1/platforms/connected", methods=["GET"])
def list_connected():
    """Public — what platforms are currently connected. Powers /ai-agents."""
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    out = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS connected_ai_platforms (
                    id SERIAL PRIMARY KEY, name TEXT NOT NULL, url TEXT,
                    type TEXT, integration_card TEXT, fit_score INTEGER,
                    status TEXT NOT NULL DEFAULT 'auto_approved',
                    source TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(name))""")
            cur.execute("""
                SELECT name, url, type, integration_card, fit_score, source,
                       created_at
                  FROM connected_ai_platforms
                 WHERE status IN ('auto_approved', 'approved')
                 ORDER BY fit_score DESC NULLS LAST, created_at DESC
                 LIMIT 200
            """)
            for name, url, ptype, card, fit, source, created in cur.fetchall():
                out.append({
                    "name": name, "url": url, "type": ptype,
                    "fit_score": fit, "source": source,
                    "integration_card_excerpt": (card or "")[:240],
                    "created_at": created.isoformat() if created else None,
                })
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error="db_error", detail=repr(e)[:200]), 500
    return jsonify(ok=True, count=len(out), platforms=out)


@ai_platform_onboarder_bp.route("/admin/ai-platforms", methods=["GET"])
def admin_dashboard():
    """Minimal HTML dashboard. Renders pending + recent + connected counts."""
    if not _admin_ok():
        return ("<h1>403</h1><p>admin key required (?admin_key=… or X-Admin-Key)</p>",
                403, {"Content-Type": "text/html"})
    c = _get_db()
    if c is None:
        return ("<h1>503</h1><p>no_db</p>", 503, {"Content-Type": "text/html"})
    pending = []
    recent = []
    connected_total = 0
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, name, url, type, contact_email, fit_score, status,
                       submitted_at
                  FROM ai_platform_submissions
                 WHERE status IN ('pending', 'new') OR status IS NULL
                 ORDER BY id LIMIT 50""")
            pending = cur.fetchall()
            cur.execute("""
                SELECT id, name, url, fit_score, status, processed_at
                  FROM ai_platform_submissions
                 WHERE status NOT IN ('pending', 'new') AND status IS NOT NULL
                 ORDER BY processed_at DESC NULLS LAST LIMIT 25""")
            recent = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM connected_ai_platforms "
                        "WHERE status IN ('auto_approved', 'approved')")
            connected_total = cur.fetchone()[0]
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return (f"<h1>db error</h1><pre>{repr(e)[:300]}</pre>", 500,
                {"Content-Type": "text/html"})

    def _row_html(cols):
        from html import escape as _esc
        return "<tr>" + "".join(
            f"<td>{_esc(str(c)) if c is not None else ''}</td>" for c in cols
        ) + "</tr>"

    body = f"""<!doctype html><html><head><title>AI Platforms · DC Hub admin</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}}
h1{{margin:0 0 8px;font-size:24px}}h2{{font-size:16px;margin:28px 0 8px;color:#555}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
th{{background:#f7f8fa;font-weight:600;font-size:11px;text-transform:uppercase;color:#666}}
.badge{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;background:#eef}}
.kpis{{display:flex;gap:18px;margin:12px 0 0}}.k{{background:#f7f8fa;padding:10px 14px;border-radius:8px;min-width:120px}}
.k .n{{font-size:22px;font-weight:700}}.k .l{{font-size:11px;color:#666;text-transform:uppercase}}
</style></head><body>
<h1>AI Platforms · Onboarder</h1>
<div class="kpis">
  <div class="k"><div class="n">{len(pending)}</div><div class="l">pending</div></div>
  <div class="k"><div class="n">{len(recent)}</div><div class="l">recent processed</div></div>
  <div class="k"><div class="n">{connected_total}</div><div class="l">connected</div></div>
</div>
<h2>Pending queue</h2>
<table><thead><tr><th>id</th><th>name</th><th>url</th><th>type</th><th>email</th>
<th>fit</th><th>status</th><th>submitted_at</th></tr></thead><tbody>
{''.join(_row_html(r) for r in pending) or '<tr><td colspan=8><em>queue empty</em></td></tr>'}
</tbody></table>
<h2>Recently processed</h2>
<table><thead><tr><th>id</th><th>name</th><th>url</th><th>fit</th>
<th>status</th><th>processed_at</th></tr></thead><tbody>
{''.join(_row_html(r) for r in recent) or '<tr><td colspan=6><em>none yet</em></td></tr>'}
</tbody></table>
<h2>Run the cron</h2>
<p><code>POST /api/v1/admin/platforms/process?admin_key=…</code> — processes up to {PER_RUN_CAP} rows.</p>
<p><code>POST /api/v1/admin/platforms/approve/&lt;id&gt;</code> · manual approve</p>
<p><code>POST /api/v1/admin/platforms/reject/&lt;id&gt;?reason=…</code> · manual reject</p>
</body></html>"""
    return (body, 200, {"Content-Type": "text/html; charset=utf-8"})


# ── Smoke ────────────────────────────────────────────────────────────
def _smoke():
    logger.info("[ai_platform_onboarder] loaded · disabled=%s · auto≥%d · enrich≥%d · cap=%d",
                _disabled(), AUTO_APPROVE_MIN, ENRICH_MIN, PER_RUN_CAP)


_smoke()
