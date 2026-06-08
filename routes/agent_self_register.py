"""routes/agent_self_register.py — Unlock 3: Self-registration API (2026-06-07).

Today an AI platform that wants to use DC Hub has to fill the /ai-agents
form and wait for human approval. Self-registration shifts that to a
public, rate-limited POST that issues a tracking key on the spot and
queues the row for the same onboarder cron (Unlock 1).

Public endpoints:
  POST /api/v1/platforms/register             — register a new platform
  GET  /api/v1/platforms/register/docs        — schema docs
  GET  /api/v1/platforms/register/<id>        — status of a previously-registered

Body (POST):
  {
    "name": "Acme Agent",
    "url":  "https://acme.example",
    "type": "mcp" | "api" | "widget",
    "contact_email": "ops@acme.example",
    "description": "1-3 sentences on what we do",
    "agent_signature": "<UA-or-User-Agent-token, optional>",
    "expected_volume_per_day": 1000
  }

Response (201):
  {
    "ok": true,
    "platform_id": 42,
    "platform_key": "dch_platform_abc123…",
    "integration_card_url": "https://dchub.cloud/integrations/acme-agent",
    "api_docs_url":         "https://dchub.cloud/api/docs",
    "recommended_quota_daily": 1000,
    "auto_approved": false,
    "fit_score": null,
    "next_step": "Your platform is in the onboarder queue. Fit score + "
                 "integration card land within 6 hours. Watch <id> via "
                 "GET /api/v1/platforms/register/<id>."
  }

Rate limit:
  10 registrations / hour per IP (in-memory ring; covers the slow trickle
  expected. If we ever scale past one Railway replica, this needs Redis).

Auto-approval:
  If fit_score (computed inline at register-time on a cheap heuristic,
  no Claude call) ≥85, status='auto_approved' immediately + connected list
  inserted. Otherwise status='pending' and the onboarder cron picks it up.

Safety:
  * Kill switch: AI_AGENT_EXPANSION_DISABLE=1 → 503 with retry-after.
  * Per-IP RL.
  * Per-email RL (5/hour) so a single email can't farm-claim slots.
  * Schema validation: name/url required, valid URL shape, email shape.
  * The platform_key namespace is `dch_platform_*` — separate from user
    `dch_*` keys, so revocation doesn't touch end-user keys.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, jsonify, request


logger = logging.getLogger(__name__)
agent_self_register_bp = Blueprint("agent_self_register", __name__)


# ── Config ───────────────────────────────────────────────────────────
RL_PER_IP_HOUR    = int(os.environ.get("SELF_REGISTER_RL_IP_HOUR",    "10"))
RL_PER_EMAIL_HOUR = int(os.environ.get("SELF_REGISTER_RL_EMAIL_HOUR", "5"))
AUTO_APPROVE_MIN  = int(os.environ.get("AI_PLATFORM_AUTO_APPROVE_MIN", "85"))
VALID_TYPES = {"mcp", "api", "widget"}


def _disabled() -> bool:
    return os.environ.get("AI_AGENT_EXPANSION_DISABLE", "").strip() == "1"


# ── DB ───────────────────────────────────────────────────────────────
def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ── Validation ───────────────────────────────────────────────────────
_URL_RE = re.compile(
    r"^https?://[A-Za-z0-9._\-]+(?:\.[A-Za-z0-9._\-]+)+(?:/[^\s]*)?$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_NAME_RE  = re.compile(r"^[\w .,&\-/'()]{2,80}$")


def _validate_payload(body: dict) -> tuple[bool, str | None, dict | None]:
    if not isinstance(body, dict):
        return False, "body_not_object", None
    name  = (body.get("name") or "").strip()
    url   = (body.get("url") or "").strip()
    ptype = (body.get("type") or "").strip().lower()
    email = (body.get("contact_email") or "").strip().lower()
    desc  = (body.get("description") or "").strip()
    sig   = (body.get("agent_signature") or "").strip()
    vol   = body.get("expected_volume_per_day")

    if not name or not _NAME_RE.match(name):
        return False, "invalid_name (2-80 chars, basic punctuation)", None
    if not url:
        return False, "url_required", None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not _URL_RE.match(url):
        return False, "invalid_url", None
    if not ptype or ptype not in VALID_TYPES:
        return False, f"invalid_type (one of {sorted(VALID_TYPES)})", None
    if not email or not _EMAIL_RE.match(email):
        return False, "invalid_contact_email", None
    if not desc or len(desc) < 10 or len(desc) > 1000:
        return False, "description_required (10-1000 chars)", None
    if sig and len(sig) > 200:
        return False, "agent_signature_too_long (<200)", None
    if vol is not None:
        try:
            vol = int(vol)
            if vol < 1 or vol > 10_000_000:
                return False, "expected_volume_per_day out of range", None
        except Exception:
            return False, "expected_volume_per_day not int", None
    return True, None, {
        "name": name, "url": url, "type": ptype,
        "contact_email": email, "description": desc,
        "agent_signature": sig or None,
        "expected_volume_per_day": vol,
    }


# ── Rate limit (in-memory) ───────────────────────────────────────────
_RL_IP: dict[str, deque] = defaultdict(deque)
_RL_EMAIL: dict[str, deque] = defaultdict(deque)
_RL_WINDOW_S = 3600


def _rl_check(bucket: dict, key: str, limit: int) -> tuple[bool, int]:
    now = time.time()
    q = bucket[key]
    while q and (now - q[0]) > _RL_WINDOW_S:
        q.popleft()
    if len(q) >= limit:
        retry = int(_RL_WINDOW_S - (now - q[0]) + 1)
        return False, retry
    q.append(now)
    return True, 0


def _client_ip() -> str:
    # Railway / Cloudflare puts the real IP in CF-Connecting-IP.
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "unknown")


# ── Recommended quota (cheap heuristic) ──────────────────────────────
def _recommend_quota(payload: dict) -> int:
    """1k/day default. MCP type → 2.5k. Big expected volume → bump."""
    base = 1000
    if payload["type"] == "mcp":
        base = 2500
    expected = payload.get("expected_volume_per_day") or 0
    if expected > base:
        # Cap at 25k/day on self-service; ops can raise via /tool-tuner.
        base = min(expected, 25000)
    return base


# ── Cheap fit score (no Claude call) ─────────────────────────────────
def _cheap_fit_score(payload: dict) -> tuple[int, list]:
    """Fast composite without an HTTP fetch. The onboarder cron later
    runs the full enrichment with URL check + Claude card."""
    s = 0
    reasons = []
    if payload["type"] == "mcp":
        s += 35; reasons.append("declared mcp")
    elif payload["type"] == "api":
        s += 25; reasons.append("declared api")
    else:
        s += 10; reasons.append("declared widget")
    if payload.get("agent_signature"):
        s += 10; reasons.append("agent_signature present")
    if (payload.get("expected_volume_per_day") or 0) >= 100:
        s += 10; reasons.append("non-trivial expected volume")
    if len(payload["description"]) >= 80:
        s += 15; reasons.append("substantial description")
    if "/" in payload["url"].split("//", 1)[-1]:
        s += 5; reasons.append("structured URL path")
    if "@" in payload["contact_email"]:
        s += 10; reasons.append("contact email valid shape")
    return min(s, 100), reasons


# ── Key generation ───────────────────────────────────────────────────
def _new_platform_key() -> str:
    """`dch_platform_<32-hex>` — separate namespace from end-user keys."""
    return "dch_platform_" + secrets.token_hex(16)


def _ensure_keys_table(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_platform_keys (
                    id SERIAL PRIMARY KEY,
                    platform_id            INTEGER,
                    api_key                TEXT NOT NULL UNIQUE,
                    issued_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    recommended_quota_daily INTEGER NOT NULL DEFAULT 1000,
                    used_today             INTEGER NOT NULL DEFAULT 0,
                    last_used_at           TIMESTAMPTZ,
                    revoked_at             TIMESTAMPTZ
                )""")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_apk_platform "
                        "ON ai_platform_keys(platform_id)")
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning("ensure ai_platform_keys failed: %s", e)
        try: c.rollback()
        except Exception: pass


def _ensure_submissions_table(c) -> None:
    """Self-heal — schema_repair adds this canonically but we don't want
    register-time to fail if the repair hasn't run yet."""
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_platform_submissions (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT,
                    type TEXT,
                    contact_email TEXT,
                    description TEXT,
                    agent_signature TEXT,
                    expected_volume_per_day INTEGER,
                    fit_score INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    integration_card TEXT,
                    enrichment_meta JSONB,
                    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ,
                    approved_at  TIMESTAMPTZ,
                    approved_by  TEXT,
                    confirmation_sent_at TIMESTAMPTZ,
                    source TEXT,
                    ip_address TEXT
                )""")
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning("ensure ai_platform_submissions failed: %s", e)
        try: c.rollback()
        except Exception: pass


# ── Endpoints ────────────────────────────────────────────────────────
@agent_self_register_bp.route("/api/v1/platforms/register", methods=["POST"])
def register():
    if _disabled():
        return jsonify(ok=False, error="self_register_disabled",
                       retry_after_seconds=3600), 503
    ip = _client_ip()
    ok_ip, retry_ip = _rl_check(_RL_IP, ip, RL_PER_IP_HOUR)
    if not ok_ip:
        return jsonify(ok=False, error="rate_limited",
                       scope="ip", retry_after_seconds=retry_ip), 429
    body = request.get_json(silent=True) or {}
    valid, err, payload = _validate_payload(body)
    if not valid:
        return jsonify(ok=False, error="invalid_payload", detail=err), 400
    ok_em, retry_em = _rl_check(_RL_EMAIL, payload["contact_email"],
                                RL_PER_EMAIL_HOUR)
    if not ok_em:
        return jsonify(ok=False, error="rate_limited",
                       scope="email", retry_after_seconds=retry_em), 429

    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    _ensure_submissions_table(c)
    _ensure_keys_table(c)

    fit_score, fit_reasons = _cheap_fit_score(payload)
    auto = fit_score >= AUTO_APPROVE_MIN
    initial_status = "auto_approved" if auto else "pending"
    quota = _recommend_quota(payload)

    # Insert the submission row.
    sub_id = None
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_platform_submissions
                    (name, url, type, contact_email, description,
                     agent_signature, expected_volume_per_day,
                     fit_score, status, source, ip_address, submitted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'self_register', %s, NOW())
                RETURNING id
            """, (payload["name"], payload["url"], payload["type"],
                  payload["contact_email"], payload["description"],
                  payload.get("agent_signature"),
                  payload.get("expected_volume_per_day"),
                  fit_score, initial_status, ip))
            sub_id = cur.fetchone()[0]
        try: c.commit()
        except Exception: pass
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("register insert failed: %s", e)
        return jsonify(ok=False, error="db_insert_failed",
                       detail=repr(e)[:200]), 500

    # Mint a tracking key.
    api_key = _new_platform_key()
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_platform_keys
                    (platform_id, api_key, recommended_quota_daily)
                VALUES (%s, %s, %s)
            """, (sub_id, api_key, quota))
        try: c.commit()
        except Exception: pass
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        logger.warning("api key insert failed: %s", e)
        # Continue — submission is more important than the tracking key.

    # If auto_approved, surface it on the connected list immediately.
    if auto:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO connected_ai_platforms
                        (name, url, type, fit_score, status, source)
                    VALUES (%s, %s, %s, %s, 'auto_approved', 'self_register')
                    ON CONFLICT (name) DO UPDATE
                       SET url = EXCLUDED.url,
                           fit_score = EXCLUDED.fit_score,
                           status = EXCLUDED.status
                """, (payload["name"], payload["url"], payload["type"],
                      fit_score))
            try: c.commit()
            except Exception: pass
        except Exception as e:
            try: c.rollback()
            except Exception: pass
            logger.warning("auto-approve connected upsert failed: %s", e)

    slug = re.sub(r"[^a-z0-9]+", "-", payload["name"].lower()).strip("-")
    integration_card_url = f"https://dchub.cloud/integrations/{slug or 'platform'}"

    return jsonify(
        ok=True,
        platform_id=sub_id,
        platform_key=api_key,
        integration_card_url=integration_card_url,
        api_docs_url="https://dchub.cloud/api/docs",
        recommended_quota_daily=quota,
        auto_approved=auto,
        fit_score=fit_score,
        fit_reasons=fit_reasons,
        next_step=("Your platform is in the onboarder queue. Full fit "
                   "score + integration card land within 6 hours. Watch "
                   f"GET /api/v1/platforms/register/{sub_id}.")
    ), 201


@agent_self_register_bp.route("/api/v1/platforms/register/<int:sub_id>",
                              methods=["GET"])
def status(sub_id: int):
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT name, url, type, fit_score, status,
                       integration_card, submitted_at, processed_at, approved_at
                  FROM ai_platform_submissions
                 WHERE id = %s
            """, (sub_id,))
            row = cur.fetchone()
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error="db_error",
                       detail=repr(e)[:200]), 500
    if not row:
        return jsonify(ok=False, error="not_found"), 404
    name, url, ptype, fit, st, card, sub_at, proc_at, app_at = row
    return jsonify(
        ok=True,
        platform_id=sub_id,
        name=name, url=url, type=ptype,
        fit_score=fit, status=st,
        integration_card=(card or "")[:2000],
        submitted_at=sub_at.isoformat() if sub_at else None,
        processed_at=proc_at.isoformat() if proc_at else None,
        approved_at=app_at.isoformat() if app_at else None,
    )


@agent_self_register_bp.route("/api/v1/platforms/register/docs", methods=["GET"])
def docs():
    """Human-readable docs for the schema."""
    body = """<!doctype html><html><head><title>DC Hub · self-register</title>
<style>body{font-family:-apple-system,sans-serif;max-width:780px;margin:24px auto;padding:0 16px;color:#222;line-height:1.55}
h1{font-size:24px}h2{font-size:18px;margin-top:28px}
pre,code{background:#f7f8fa;border:1px solid #eee;border-radius:6px;padding:2px 5px;font-size:13px}
pre{padding:14px;overflow:auto}.note{background:#fff8e1;padding:10px 14px;border-radius:6px;font-size:14px;margin:10px 0}
table{border-collapse:collapse}td,th{padding:5px 9px;border-bottom:1px solid #eee;font-size:14px;vertical-align:top;text-align:left}
</style></head><body>
<h1>DC Hub · platform self-registration</h1>
<p>Public, rate-limited POST. Use this to register an AI platform that wants
to query DC Hub's data layer (21,000+ facilities, 300+ markets, 38 MCP tools).</p>
<h2>POST <code>/api/v1/platforms/register</code></h2>
<table><tr><th>field</th><th>type</th><th>required</th><th>notes</th></tr>
<tr><td>name</td><td>string</td><td>yes</td><td>2-80 chars</td></tr>
<tr><td>url</td><td>string</td><td>yes</td><td>https://… (http auto-upgraded)</td></tr>
<tr><td>type</td><td>enum</td><td>yes</td><td><code>mcp</code> | <code>api</code> | <code>widget</code></td></tr>
<tr><td>contact_email</td><td>string</td><td>yes</td><td>delivery for confirmation</td></tr>
<tr><td>description</td><td>string</td><td>yes</td><td>10-1000 chars; what you do</td></tr>
<tr><td>agent_signature</td><td>string</td><td>no</td><td>your User-Agent token</td></tr>
<tr><td>expected_volume_per_day</td><td>integer</td><td>no</td><td>1 - 10,000,000</td></tr>
</table>
<h2>Response</h2>
<pre>{
  "ok": true,
  "platform_id": 42,
  "platform_key": "dch_platform_…",
  "integration_card_url": "https://dchub.cloud/integrations/<slug>",
  "api_docs_url": "https://dchub.cloud/api/docs",
  "recommended_quota_daily": 1000,
  "auto_approved": false,
  "fit_score": 65,
  "next_step": "Your platform is in the onboarder queue. …"
}</pre>
<div class="note"><strong>Auto-approval</strong> kicks in at fit_score ≥85.
Otherwise the row enters the manual queue and the onboarder cron runs the
full enrichment (URL check + Claude integration card) within 6 hours.</div>
<h2>Rate limits</h2>
<ul><li>10 / hour per IP</li><li>5 / hour per contact_email</li></ul>
<h2>Status check</h2>
<p><code>GET /api/v1/platforms/register/&lt;id&gt;</code> — current fit_score
+ integration_card preview.</p>
<h2>Issued key</h2>
<p>The returned <code>platform_key</code> uses the <code>dch_platform_*</code>
namespace, separate from end-user keys. Use it on MCP / REST calls via
<code>X-API-Key: dch_platform_…</code>; the recommended quota is the
suggested daily call ceiling.</p>
</body></html>"""
    return (body, 200, {"Content-Type": "text/html; charset=utf-8"})


# ── Smoke ────────────────────────────────────────────────────────────
def _smoke():
    logger.info("[agent_self_register] loaded · disabled=%s · RL ip=%d/h email=%d/h",
                _disabled(), RL_PER_IP_HOUR, RL_PER_EMAIL_HOUR)


_smoke()
