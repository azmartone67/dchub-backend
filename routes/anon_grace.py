"""Phase EEEEE (2026-05-16) — anon grace mode (volume recovery).

User: "before we were iterating we had 60k inquiries... so we need
to figure this out, this is paramount"

7d volume dropped from 60K → 37K (~38%) after XXX tightened FREE
tier (50→25/day, 5→3 rows) and moved search_facilities + get_news
to IDENTIFIED. Many anon agents bounced off the 402 instead of
claiming a key. DDDDD shipped auto-mint trial keys (returned IN
the 402) but agents still need to PARSE the response + retry.

EEEEE goes further: **never bounce anon callers off paid tools at
all** for the first 5 calls in a 24h window per (ip+ua). The first
call succeeds with data; the trial key is included in the response
metadata for the agent to pick up. Zero perceived friction.

  GET  /api/v1/grace/status      public — current grace cap + remaining
  POST /api/v1/grace/check       admin — debug a specific caller
  GET  /api/v1/grace/stats       public — funnel metrics

How it integrates with mcp_gatekeeper._gate():
  1. Anon caller hits IDENTIFIED gate
  2. Check anon_grace_log for this (ip+ua) in last 24h
  3. If grace_used < 5: ALLOW THE CALL (return None, no 402), mint
     trial key in background, attach as response metadata
  4. If grace_used >= 5: fall through to DDDDD auto-mint paywall

Volume target: restore 60K weekly inquiries with conversion still
captured via the trial key issued silently.
"""

from __future__ import annotations

import os
import datetime
import hashlib
from flask import Blueprint, jsonify, request


anon_grace_bp = Blueprint("anon_grace", __name__)


_ADMIN_KEY  = (os.environ.get("DCHUB_ADMIN_KEY")
               or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
# Phase NN (2026-05-17) — bump default 5 → 25. Phase HH revealed the
# truth: agents getting trial keys but never coming back (0 auto-trial
# conversions on 7,769 paywall hits). Generous grace means more agents
# get DATA without ever seeing a paywall AND more chances to discover
# the auto_trial_key field in success-response metadata. The 5-cap was
# too aggressive — most agents bounce off the paywall within their
# first session before they have a chance to learn the conversion path.
# Env override `DCHUB_ANON_GRACE_CAP` still works for ops dial-back.
_GRACE_CAP_PER_24H = int(os.environ.get("DCHUB_ANON_GRACE_CAP", "25"))


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS anon_grace_log (
    id              BIGSERIAL PRIMARY KEY,
    caller_hash     TEXT NOT NULL,         -- sha256(ip+ua):16
    tool            TEXT,
    granted         BOOLEAN NOT NULL DEFAULT TRUE,
    auto_trial_key  TEXT,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_grace_caller_time
    ON anon_grace_log(caller_hash, granted_at DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _caller_hash(req=None) -> str:
    req = req or request
    ip = (req.headers.get("CF-Connecting-IP")
          or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or req.remote_addr or "?")
    ua = (req.headers.get("User-Agent") or "")[:200]
    return hashlib.sha256((ip + "|" + ua).encode()).hexdigest()[:16]


def grace_remaining(req=None) -> int:
    """Returns calls remaining in the 24h grace window. 0 = exhausted."""
    h = _caller_hash(req)
    c = _conn()
    if c is None: return 0  # fail-safe: no grace if DB unreachable
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM anon_grace_log
                     WHERE caller_hash = %s
                       AND granted = TRUE
                       AND granted_at >= NOW() - INTERVAL '24 hours'
                """, (h,))
                used = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                return 0
    finally:
        try: c.close()
        except Exception: pass
    return max(0, _GRACE_CAP_PER_24H - used)


def consume_grace(req=None, tool: str = "", trial_key: str | None = None) -> bool:
    """Call when granting grace. Logs the grant + returns True. If
    grace is already exhausted, returns False without logging."""
    if grace_remaining(req) <= 0:
        return False
    h = _caller_hash(req)
    c = _conn()
    if c is None: return False
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO anon_grace_log
                      (caller_hash, tool, granted, auto_trial_key)
                    VALUES (%s, %s, TRUE, %s)
                    ON CONFLICT DO NOTHING
                """, (h, (tool or "")[:40] or None, (trial_key or "")[:64] or None))
                return True
            except Exception:
                return False
    finally:
        try: c.close()
        except Exception: pass


def grace_metadata(req=None) -> dict | None:
    """Returns metadata to attach to a graced response so the agent
    sees the trial key + remaining grace + path to permanent."""
    remaining = grace_remaining(req)
    if remaining <= 0:
        return None
    return {
        "anon_grace_calls_remaining": remaining,
        "anon_grace_cap":             _GRACE_CAP_PER_24H,
        "anon_grace_hint": ("You have free anon access for "
                             f"{_GRACE_CAP_PER_24H} calls per 24h. After that, "
                             f"a trial key auto-mints. Claim a permanent free "
                             f"key now via POST /api/v1/keys/claim — never "
                             f"hits this cap."),
    }


@anon_grace_bp.route("/api/v1/grace/status", methods=["GET"])
def status():
    """Public — caller checks their remaining grace."""
    remaining = grace_remaining(request)
    return jsonify({
        "remaining":  remaining,
        "cap":        _GRACE_CAP_PER_24H,
        "window":     "24h",
        "exhausted":  remaining == 0,
        "next_step": ("you have free anon access remaining" if remaining > 0
                       else "claim a key at POST /api/v1/keys/claim for unlimited access"),
    }), 200


@anon_grace_bp.route("/api/v1/grace/stats", methods=["GET"])
def stats():
    """Public — overall grace funnel."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out = {"grants_total": 0, "grants_24h": 0, "grants_7d": 0,
           "unique_callers_24h": 0, "unique_callers_7d": 0,
           "tools_using_grace": []}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE granted_at >= NOW() - INTERVAL '24 hours') AS h24,
                           COUNT(*) FILTER (WHERE granted_at >= NOW() - INTERVAL '7 days') AS d7,
                           COUNT(DISTINCT caller_hash) FILTER (WHERE granted_at >= NOW() - INTERVAL '24 hours') AS u24,
                           COUNT(DISTINCT caller_hash) FILTER (WHERE granted_at >= NOW() - INTERVAL '7 days') AS u7
                      FROM anon_grace_log
                """)
                r = cur.fetchone() or (0, 0, 0, 0, 0)
                out["grants_total"]       = int(r[0] or 0)
                out["grants_24h"]         = int(r[1] or 0)
                out["grants_7d"]          = int(r[2] or 0)
                out["unique_callers_24h"] = int(r[3] or 0)
                out["unique_callers_7d"]  = int(r[4] or 0)
                cur.execute("""
                    SELECT tool, COUNT(*) FROM anon_grace_log
                     WHERE granted_at >= NOW() - INTERVAL '7 days'
                       AND tool IS NOT NULL
                     GROUP BY tool ORDER BY COUNT(*) DESC LIMIT 10
                """)
                out["tools_using_grace"] = [
                    {"tool": r[0], "grants": int(r[1] or 0)}
                    for r in cur.fetchall()
                ]
            except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    out["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
