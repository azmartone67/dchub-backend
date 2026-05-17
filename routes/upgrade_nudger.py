"""Phase WWWW (2026-05-16) — tier-upgrade nudger.

Direct revenue lift. When an IDENTIFIED user is hitting their cap
heavily, that's a clear upgrade-DEVELOPER signal — they're getting
value. Auto-email a friendly one-click upgrade prompt.

  POST /api/v1/nudges/send-pending   admin cron entry
  GET  /api/v1/nudges/log            public sent history

Trigger:
  - IDENTIFIED tier (200 calls/day cap)
  - Hit >=80% (160+ calls) on 3 OR MORE of the last 7 days
  - Not nudged in last 14 days

Email via Resend with one-click Stripe link prefilled with their
email. Tracks in tier_upgrade_nudges_sent so we don't spam.

Cron: .github/workflows/upgrade-nudge-weekly.yml fires Tuesday
15:00 UTC (after Monday digests, before mid-week).
"""

from __future__ import annotations

import os
import datetime
import hashlib
from flask import Blueprint, jsonify, request


upgrade_nudger_bp = Blueprint("upgrade_nudger", __name__)


_ADMIN_KEY  = (os.environ.get("DCHUB_ADMIN_KEY")
               or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY")
               or os.environ.get("RESEND_API_KEY") or "").strip()
_FROM_NAME  = os.environ.get("DCHUB_FROM_NAME",  "DC Hub")
_FROM_EMAIL = os.environ.get("DCHUB_FROM_EMAIL", "alerts@dchub.cloud")
_STRIPE_DEV = "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c"
_IDENTIFIED_DAILY_CAP = 200
_NUDGE_THRESHOLD_PCT  = 0.80   # 80% of cap
_NUDGE_MIN_DAYS       = 3      # heavy use on 3+ of last 7d
_NUDGE_COOLDOWN_DAYS  = 14


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
CREATE TABLE IF NOT EXISTS tier_upgrade_nudges_sent (
    id              BIGSERIAL PRIMARY KEY,
    api_key_hash    TEXT NOT NULL,        -- sha256(api_key):16
    user_email      TEXT,
    current_tier    TEXT NOT NULL,
    suggested_tier  TEXT NOT NULL,
    heavy_days_7d   INT NOT NULL,
    peak_day_calls  INT,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT NOT NULL DEFAULT 'queued',
    delivery_info   TEXT
);
CREATE INDEX IF NOT EXISTS ix_tier_nudges_hash_sent
    ON tier_upgrade_nudges_sent(api_key_hash, sent_at DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _send_via_resend(to_email: str, subject: str, body_html: str) -> tuple[bool, str]:
    if not _RESEND_KEY: return False, "no_resend_key"
    try:
        import requests
        r = requests.post(
            "https://api.resend.com/emails",
            json={"from": f"{_FROM_NAME} <{_FROM_EMAIL}>",
                  "to": [to_email], "subject": subject, "html": body_html},
            headers={"Authorization": f"Bearer {_RESEND_KEY}"},
            timeout=10,
        )
        return (r.status_code < 300), (f"sent_{r.status_code}" if r.status_code < 300
                                         else f"status_{r.status_code}_{r.text[:80]}")
    except Exception as e:
        return False, f"{type(e).__name__}:{str(e)[:60]}"


def _render_nudge_html(email: str, heavy_days: int, peak: int) -> str:
    cap_pct = int(100 * peak / _IDENTIFIED_DAILY_CAP) if peak else 0
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;max-width:600px;
margin:0 auto;padding:1.5rem;color:#1f2937;line-height:1.55">
<div style="background:linear-gradient(135deg,#065f46,#0f766e);color:white;padding:1.25rem;border-radius:8px;margin-bottom:1.5rem">
 <h2 style="margin:0;font-size:1.2rem">⚡ You're getting real value from DC Hub</h2>
 <p style="margin:.25rem 0 0;color:#d1fae5;font-size:.9rem">{heavy_days} heavy-use days last week · peak {peak} calls ({cap_pct}% of your 200/day cap)</p>
</div>
<p>Your usage pattern shows you're hitting the daily limit regularly on data-center research workflows. The DEVELOPER plan gives you:</p>
<ul style="line-height:1.7">
 <li><strong>2,000 calls/day</strong> (10× your current cap)</li>
 <li><strong>100 rows per call</strong> (vs 20 today)</li>
 <li><strong>Full transaction CSV export</strong> at /api/v1/transactions/export.csv</li>
 <li><strong>analyze_site MCP tool</strong> — composite lat/lon scorer</li>
 <li><strong>No rate-limit cooldown</strong> between calls</li>
</ul>
<p style="margin-top:1.5rem">
 <a href="{_STRIPE_DEV}?prefilled_email={email}" style="display:inline-block;background:linear-gradient(135deg,#065f46,#0f766e);color:white;padding:.7rem 1.5rem;border-radius:6px;font-weight:700;text-decoration:none">
   Upgrade to DEVELOPER — $49/mo →
 </a>
</p>
<p style="color:#6b7280;font-size:.85rem;margin-top:1.5rem">
 Cancel anytime · No long-term contract · Your existing key keeps working<br>
 Questions? <a href="mailto:api@dchub.cloud" style="color:#1e40af">api@dchub.cloud</a>
</p>
</body></html>"""


def find_eligible_users(cur) -> list[dict]:
    """Returns list of (api_key_hash, user_email, heavy_days_7d, peak)
    for IDENTIFIED users hitting cap >=3 of last 7 days."""
    out = []
    # mcp_call_log schema can vary across deploys; use defensive try
    threshold = int(_IDENTIFIED_DAILY_CAP * _NUDGE_THRESHOLD_PCT)
    try:
        cur.execute("""
            SELECT to_regclass('public.mcp_call_log'),
                   to_regclass('public.api_keys')
        """)
        regs = cur.fetchone() or [None, None]
        if not (regs[0] and regs[1]):
            return out
    except Exception:
        return out
    try:
        cur.execute(f"""
            WITH daily AS (
              SELECT api_key,
                     DATE(timestamp) AS d,
                     COUNT(*) AS n
                FROM mcp_call_log
               WHERE timestamp >= NOW() - INTERVAL '7 days'
                 AND api_key IS NOT NULL
               GROUP BY api_key, DATE(timestamp)
            ),
            heavy AS (
              SELECT api_key,
                     COUNT(*) AS heavy_days,
                     MAX(n)   AS peak
                FROM daily
               WHERE n >= {threshold}
               GROUP BY api_key
              HAVING COUNT(*) >= {_NUDGE_MIN_DAYS}
            )
            SELECT h.api_key, h.heavy_days, h.peak,
                   ak.rate_limit_tier, ak.metadata
              FROM heavy h
              JOIN api_keys ak ON ak.key_prefix = LEFT(h.api_key, 16)
                                OR ak.key_hash = h.api_key
             WHERE LOWER(COALESCE(ak.rate_limit_tier, 'free')) = 'identified'
             LIMIT 50
        """)
        rows = cur.fetchall()
    except Exception:
        return out
    for r in rows:
        api_key = r[0]
        heavy = int(r[1] or 0)
        peak = int(r[2] or 0)
        meta = r[4] or {}
        if isinstance(meta, str):
            try:
                import json as _j; meta = _j.loads(meta)
            except Exception: meta = {}
        email = (meta.get("email") or meta.get("user_email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        key_hash = hashlib.sha256(str(api_key).encode()).hexdigest()[:16]
        out.append({
            "api_key_hash":   key_hash,
            "user_email":     email,
            "heavy_days_7d":  heavy,
            "peak_day_calls": peak,
        })
    return out


def send_pending_nudges(dry_run: bool = False) -> dict:
    out: dict = {"sent": [], "skipped": [], "errors": [],
                 "dry_run": dry_run,
                 "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}
    c = _conn()
    if c is None:
        out["errors"].append("no_database"); return out
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor() as cur:
            eligible = find_eligible_users(cur)
            out["eligible_count"] = len(eligible)
            for u in eligible:
                # Cooldown check
                try:
                    cur.execute(f"""
                        SELECT 1 FROM tier_upgrade_nudges_sent
                         WHERE api_key_hash = %s
                           AND sent_at >= NOW() - INTERVAL '{_NUDGE_COOLDOWN_DAYS} days'
                         LIMIT 1
                    """, (u["api_key_hash"],))
                    if cur.fetchone():
                        out["skipped"].append({"hash": u["api_key_hash"],
                                                "reason": "cooldown_14d"})
                        continue
                except Exception:
                    pass
                subject = (f"You're using DC Hub heavily — "
                           f"DEVELOPER tier ($49/mo) might fit better")
                body = _render_nudge_html(u["user_email"], u["heavy_days_7d"],
                                            u["peak_day_calls"])
                if dry_run:
                    out["sent"].append({**u, "dry_run": True, "subject": subject})
                    continue
                ok, info = _send_via_resend(u["user_email"], subject, body)
                try:
                    cur.execute("""
                        INSERT INTO tier_upgrade_nudges_sent
                          (api_key_hash, user_email, current_tier,
                           suggested_tier, heavy_days_7d, peak_day_calls,
                           status, delivery_info)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                    """, (u["api_key_hash"], u["user_email"],
                          "IDENTIFIED", "DEVELOPER",
                          u["heavy_days_7d"], u["peak_day_calls"],
                          "sent" if ok else "send_failed", info))
                except Exception: pass
                (out["sent"] if ok else out["errors"]).append({**u, "info": info})
    finally:
        try: c.close()
        except Exception: pass
    return out


@upgrade_nudger_bp.route("/api/v1/nudges/send-pending", methods=["POST"])
def send_pending():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    dry = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    return jsonify(send_pending_nudges(dry_run=dry)), 200


@upgrade_nudger_bp.route("/api/v1/nudges/log", methods=["GET"])
def nudges_log():
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT api_key_hash, current_tier, suggested_tier,
                       heavy_days_7d, peak_day_calls, sent_at, status
                  FROM tier_upgrade_nudges_sent
                 ORDER BY sent_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "api_key_hash":  r["api_key_hash"],
        "current_tier":  r["current_tier"],
        "suggested_tier":r["suggested_tier"],
        "heavy_days_7d": int(r["heavy_days_7d"] or 0),
        "peak_day_calls":int(r["peak_day_calls"] or 0),
        "sent_at":       r["sent_at"].isoformat() if r["sent_at"] else None,
        "status":        r["status"],
    } for r in rows]
    return jsonify(log=out, count=len(out)), 200
