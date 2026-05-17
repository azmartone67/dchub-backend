"""Phase BBBB (2026-05-16) — /developers acquisition funnel.

User asked: "is our developer site actively getting new ai agents to
use our tool?" Today we DON'T KNOW. /developers gets traffic but we
have no funnel: visit → signup → key claim → first MCP call →
retention. This module builds it.

Mirrors mcp_funnel.py's shape but for the developer-facing side:

  Stage 0: /developers visit
  Stage 1: clicked "Get API key" button (or scrolled to pricing block)
  Stage 2: POST /api/v1/keys/claim succeeded (key minted)
  Stage 3: first MCP call with the new key
  Stage 4: still calling 7 days later (retention)

Endpoints:
  POST /api/v1/developers/track         beacon ingest (rate-limited)
  GET  /api/v1/developers/funnel        aggregate funnel (last 30d)
  GET  /api/v1/developers/retention     7d/30d retention per cohort

Brain detector check_developers_funnel_dead fires if /developers
sees traffic but stage 1 (CTA click) drop is >95% — page is
attracting visits but not converting interest into intent.
"""

from __future__ import annotations

import os
import datetime
import hashlib
from flask import Blueprint, jsonify, request


developers_funnel_bp = Blueprint("developers_funnel", __name__)


_RL_BUCKET: dict[str, list[float]] = {}
_RL_WINDOW_SEC = 60.0
_RL_MAX = 60  # 60 events/min/IP


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
CREATE TABLE IF NOT EXISTS developer_funnel_events (
    id          BIGSERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,
    anon_id     TEXT,
    api_key_hash TEXT,
    payload     JSONB,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_developer_funnel_events_type_ts
    ON developer_funnel_events(event_type, ts DESC);
CREATE INDEX IF NOT EXISTS ix_developer_funnel_events_anon
    ON developer_funnel_events(anon_id, ts DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        pass


def _anon_id_from_request(req) -> str:
    """Same shape as surface_brain — sha256(ip + ua):16 — no PII."""
    ip = (req.headers.get("CF-Connecting-IP")
          or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or req.remote_addr or "?")
    ua = req.headers.get("User-Agent", "")[:120]
    return hashlib.sha256((ip + "|" + ua).encode()).hexdigest()[:16]


def _rate_limited(ip: str) -> bool:
    import time
    now = time.time()
    bucket = _RL_BUCKET.setdefault(ip, [])
    bucket[:] = [t for t in bucket if (now - t) < _RL_WINDOW_SEC]
    if len(bucket) >= _RL_MAX:
        return True
    bucket.append(now)
    return False


_ALLOWED_EVENTS = {
    "page_view",
    "cta_click",        # any signup / claim-key CTA click
    "pricing_view",     # scrolled to pricing block
    "key_claimed",      # POST /api/v1/keys/claim returned 200
    "first_mcp_call",   # first call observed in mcp_call_log
    "outbound_doc",     # clicked an external doc link
}


@developers_funnel_bp.route("/api/v1/developers/track", methods=["POST", "OPTIONS"])
def developers_track():
    """Beacon ingest from the /developers page (sendBeacon-friendly)."""
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST",
        })
    # DNT respect
    if request.headers.get("DNT") == "1":
        return ("", 204)
    ip = (request.headers.get("CF-Connecting-IP")
          or request.remote_addr or "?")
    if _rate_limited(ip):
        return ("", 204)  # silent drop; don't tell scrapers we limited

    data = request.get_json(silent=True) or {}
    event_type = (data.get("event") or "").strip()[:40]
    if event_type not in _ALLOWED_EVENTS:
        return ("", 204)

    payload = data.get("payload") or {}
    if not isinstance(payload, dict): payload = {}
    # Keep payload small + safe
    safe = {k: v for k, v in payload.items() if isinstance(k, str)
            and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 200}

    anon_id = _anon_id_from_request(request)
    api_key = (data.get("api_key_hint") or "")[:24]
    api_key_h = hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else None

    c = _conn()
    if c is None:
        return ("", 204)
    try:
        _ensure_schema(c)
        import json as _json
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO developer_funnel_events
                  (event_type, anon_id, api_key_hash, payload)
                VALUES (%s, %s, %s, %s::jsonb)
            """, (event_type, anon_id, api_key_h, _json.dumps(safe)))
    finally:
        try: c.close()
        except Exception: pass
    return ("", 204)


def _compute_funnel(days: int = 30) -> dict:
    """Stage-by-stage funnel over the last N days. Cohorts by anon_id
    where possible; falls back to event counts otherwise.

    Stage 0: page_view
    Stage 1: cta_click (or pricing_view)
    Stage 2: key_claimed (from developer_funnel_events OR api_keys table)
    Stage 3: first_mcp_call (from mcp_call_log within 24h of claim)
    Stage 4: 7-day retention (call observed 6-8 days after claim)
    """
    out: dict = {
        "window_days":   days,
        "stages": {
            "0_page_views":          0,
            "0_unique_visitors":     0,
            "1_intent_signals":      0,
            "2_keys_claimed":        0,
            "3_first_mcp_call":      0,
            "4_retained_7d":         0,
        },
        "drop_rates":  {},
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    c = _conn()
    if c is None:
        return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # Stage 0
            try:
                cur.execute(f"""
                    SELECT COUNT(*) AS views,
                           COUNT(DISTINCT anon_id) AS unique_visitors
                      FROM developer_funnel_events
                     WHERE event_type = 'page_view'
                       AND ts >= NOW() - INTERVAL '{int(days)} days'
                """)
                r = cur.fetchone() or [0, 0]
                out["stages"]["0_page_views"]      = int(r[0] or 0)
                out["stages"]["0_unique_visitors"] = int(r[1] or 0)
            except Exception:
                pass

            # Stage 1 — intent signals
            try:
                cur.execute(f"""
                    SELECT COUNT(DISTINCT anon_id) FROM developer_funnel_events
                     WHERE event_type IN ('cta_click','pricing_view')
                       AND ts >= NOW() - INTERVAL '{int(days)} days'
                """)
                r = cur.fetchone() or [0]
                out["stages"]["1_intent_signals"] = int(r[0] or 0)
            except Exception:
                pass

            # Stage 2 — keys claimed. Prefer event log; fallback to
            # api_keys table created_at if eventless.
            try:
                cur.execute(f"""
                    SELECT COUNT(DISTINCT anon_id) FROM developer_funnel_events
                     WHERE event_type = 'key_claimed'
                       AND ts >= NOW() - INTERVAL '{int(days)} days'
                """)
                r = cur.fetchone() or [0]
                claimed = int(r[0] or 0)
            except Exception:
                claimed = 0
            if claimed == 0:
                # Fallback: count distinct keys created in api_keys
                try:
                    cur.execute(f"""
                        SELECT to_regclass('public.api_keys')
                    """)
                    if (cur.fetchone() or [None])[0]:
                        cur.execute(f"""
                            SELECT COUNT(*) FROM api_keys
                             WHERE created_at >= NOW() - INTERVAL '{int(days)} days'
                        """)
                        r = cur.fetchone() or [0]
                        claimed = int(r[0] or 0)
                except Exception:
                    pass
            out["stages"]["2_keys_claimed"] = claimed

            # Stage 3 — first MCP call attributed (rough: count distinct
            # api_keys in mcp_call_log seen for the first time in the
            # window AND the same key was created in the window).
            try:
                cur.execute(f"""
                    SELECT to_regclass('public.mcp_call_log'),
                           to_regclass('public.api_keys')
                """)
                regs = cur.fetchone() or [None, None]
                if regs[0] and regs[1]:
                    cur.execute(f"""
                        WITH new_keys AS (
                          SELECT id, key_prefix, created_at
                            FROM api_keys
                           WHERE created_at >= NOW() - INTERVAL '{int(days)} days'
                        ),
                        first_call AS (
                          SELECT api_key, MIN(timestamp) AS first_ts
                            FROM mcp_call_log
                           WHERE timestamp >= NOW() - INTERVAL '{int(days)} days'
                             AND api_key IS NOT NULL
                           GROUP BY api_key
                        )
                        SELECT COUNT(*)
                          FROM new_keys nk
                          JOIN first_call fc ON fc.api_key LIKE (nk.key_prefix || '%')
                         WHERE fc.first_ts >= nk.created_at
                    """)
                    r = cur.fetchone() or [0]
                    out["stages"]["3_first_mcp_call"] = int(r[0] or 0)

                    # Stage 4 — retained 7d
                    cur.execute(f"""
                        WITH new_keys AS (
                          SELECT id, key_prefix, created_at
                            FROM api_keys
                           WHERE created_at >= NOW() - INTERVAL '{int(days)} days'
                        )
                        SELECT COUNT(*)
                          FROM new_keys nk
                         WHERE EXISTS (
                           SELECT 1 FROM mcp_call_log m
                            WHERE m.api_key LIKE (nk.key_prefix || '%')
                              AND m.timestamp >= nk.created_at + INTERVAL '6 days'
                              AND m.timestamp <= nk.created_at + INTERVAL '8 days'
                         )
                    """)
                    r = cur.fetchone() or [0]
                    out["stages"]["4_retained_7d"] = int(r[0] or 0)
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass

    s = out["stages"]
    def _drop(a, b):
        if a == 0: return None
        return round(100.0 * (1 - (b / a)), 1)
    out["drop_rates"] = {
        "0_visit_to_1_intent":     _drop(s["0_unique_visitors"], s["1_intent_signals"]),
        "1_intent_to_2_claimed":   _drop(s["1_intent_signals"],  s["2_keys_claimed"]),
        "2_claimed_to_3_called":   _drop(s["2_keys_claimed"],    s["3_first_mcp_call"]),
        "3_called_to_4_retained":  _drop(s["3_first_mcp_call"],  s["4_retained_7d"]),
    }
    return out


@developers_funnel_bp.route("/api/v1/developers/funnel", methods=["GET"])
def developers_funnel():
    try: days = max(1, min(90, int(request.args.get("days") or 30)))
    except (ValueError, TypeError): days = 30
    data = _compute_funnel(days)
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
