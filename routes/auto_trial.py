"""Phase DDDDD (2026-05-16) — auto-mint trial keys to kill paywall friction.

User diagnosis: 7,839 paywall signals over 7 days → 6 conversions
over 30 days. That's **0.08%** conversion. 100+ distinct users
hammering `get_grid_intelligence` + `get_fiber_intel` and bouncing
off the paywall instead of claiming a key.

Root cause: the paywall response TELLS agents to POST to
/api/v1/keys/claim, but most agents either don't parse the JSON,
or relay the natural-language message to a human who walks away.

The fix: **mint a working IDENTIFIED-tier trial key INLINE in the
paywall response.** Agent gets the key in the same response,
retries with X-API-Key header, succeeds. Conversion happens
WITHOUT a human signup step.

Auto-trial keys:
  - Prefix: `dch_trial_`
  - Resolved by mcp_gatekeeper as IDENTIFIED tier
  - 200 calls/day cap (same as IDENTIFIED) — abuse risk is bounded
  - 30-day expiry; agent can convert to permanent via
    POST /api/v1/keys/auto-trial/redeem {email}
  - Tracked in auto_trial_keys table for funnel attribution

  POST /api/v1/keys/auto-mint              admin or called inline by gatekeeper
  POST /api/v1/keys/auto-trial/redeem      bind trial key to email (one-click conv)
  GET  /api/v1/keys/auto-trial/stats       public funnel metrics

Brain detector check_auto_trial_conversion_rate fires if <20% of
trial keys → real signups within 7 days. Tracks the fix's impact.
"""

from __future__ import annotations

import os
import secrets
import datetime
import hashlib
from flask import Blueprint, jsonify, request


auto_trial_bp = Blueprint("auto_trial", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


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


# Phase ZZZZ-trial-tighten (2026-05-18): trial config in one place.
# Brain narrative + funnel showed: 15,104 paywall signals → 0 conversions
# because the trial gave 200 calls/day for 30 days — long + generous
# enough that agents never had to upgrade.
# v2: 7-day expiry + 50 calls/day. Forces renewal/upgrade decision quicker.
TRIAL_DAYS         = 7
TRIAL_DAILY_CALLS  = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_trial_keys (
    api_key          TEXT PRIMARY KEY,
    minted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '7 days'),
    minted_for_tool  TEXT,
    request_ip_hash  TEXT,
    request_ua       TEXT,
    last_used_at     TIMESTAMPTZ,
    call_count       INT NOT NULL DEFAULT 0,
    signed_up_email  TEXT,
    upgraded_tier    TEXT,
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS ix_auto_trial_ip ON auto_trial_keys(request_ip_hash);
CREATE INDEX IF NOT EXISTS ix_auto_trial_signedup ON auto_trial_keys(signed_up_email)
    WHERE signed_up_email IS NOT NULL;
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def mint_trial_for_request(req=None, tool_name: str = "") -> dict:
    """Called by mcp_gatekeeper when an anonymous user hits an
    IDENTIFIED-tier gate. Returns {api_key, expires_at, cap, ...}.

    Reuses an existing trial key for the SAME (ip_hash, ua) within
    the last 24h instead of minting a new one — prevents N-keys-per-
    user when an agent retries before getting the message."""
    req = req or request
    ip = (req.headers.get("CF-Connecting-IP")
          or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or req.remote_addr or "?")
    ua = (req.headers.get("User-Agent") or "")[:200]
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]

    c = _conn()
    if c is None:
        return {"error": "no_database", "ok": False}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # Check for existing recent trial key for this caller
            try:
                cur.execute("""
                    SELECT api_key, expires_at FROM auto_trial_keys
                     WHERE request_ip_hash = %s
                       AND request_ua = %s
                       AND minted_at >= NOW() - INTERVAL '24 hours'
                       AND expires_at > NOW()
                     ORDER BY minted_at DESC LIMIT 1
                """, (ip_hash, ua))
                r = cur.fetchone()
                if r:
                    # Compute days_remaining for the countdown CTA
                    import datetime as _dt
                    days_left = None
                    if r[1]:
                        delta = r[1] - _dt.datetime.now(_dt.timezone.utc)
                        days_left = max(0, int(delta.total_seconds() / 86400))
                    return {
                        "ok":          True,
                        "api_key":     r[0],
                        "expires_at":  r[1].isoformat() if r[1] else None,
                        "tier":        "IDENTIFIED",
                        "daily_calls": TRIAL_DAILY_CALLS,
                        "trial_days":  TRIAL_DAYS,
                        "days_remaining": days_left,
                        "reused":      True,
                        "upgrade_cta": (
                            f"Trial expires in {days_left} day(s). "
                            f"Developer plan ($9/mo) = 500 calls/day permanent, "
                            f"no expiry. https://buy.stripe.com/14k14og7w7Zz9KJ8i6aZi02"
                        ),
                        "instructions":(f"Use api_key in X-API-Key header. "
                                         f"FREE for {days_left} more day(s) at "
                                         f"{TRIAL_DAILY_CALLS}/day. Upgrade for unlimited."),
                    }
            except Exception: pass

            # Mint a new trial key
            api_key = "dch_trial_" + secrets.token_urlsafe(24).replace("_", "x").replace("-", "x")[:32]
            try:
                cur.execute(f"""
                    INSERT INTO auto_trial_keys
                      (api_key, minted_for_tool, request_ip_hash, request_ua,
                       expires_at)
                    VALUES (%s, %s, %s, %s, NOW() + INTERVAL '{TRIAL_DAYS} days')
                    ON CONFLICT (api_key) DO NOTHING
                    RETURNING expires_at
                """, (api_key, tool_name[:40] or None, ip_hash, ua))
                r = cur.fetchone()
                expires = r[0].isoformat() if r and r[0] else None
            except Exception:
                return {"error": "mint_failed", "ok": False}
    finally:
        try: c.close()
        except Exception: pass

    return {
        "ok":          True,
        "api_key":     api_key,
        "expires_at":  expires,
        "tier":        "IDENTIFIED",
        "daily_calls": TRIAL_DAILY_CALLS,
        "trial_days":  TRIAL_DAYS,
        "days_remaining": TRIAL_DAYS,
        "reused":      False,
        "upgrade_cta": (
            f"Trial gives you {TRIAL_DAILY_CALLS} calls/day for "
            f"{TRIAL_DAYS} days. After that you'll need to upgrade. "
            f"Skip the wait — Developer plan ($9/mo) starts at 500 "
            f"calls/day with no expiry. "
            f"https://buy.stripe.com/14k14og7w7Zz9KJ8i6aZi02"
        ),
        "instructions":(f"Use api_key in X-API-Key header. FREE for "
                         f"{TRIAL_DAYS} days at {TRIAL_DAILY_CALLS}/day. "
                         f"To extend + persist to your account: POST "
                         f"/api/v1/keys/auto-trial/redeem {{api_key, email}}."),
    }


def is_trial_key(api_key: str) -> bool:
    """Cheap shape check. mcp_gatekeeper.resolve_tier delegates to this."""
    return bool(api_key) and api_key.startswith("dch_trial_")


def validate_trial_key(api_key: str) -> tuple[bool, str]:
    """Returns (valid, reason). Validates against DB + expiry."""
    if not is_trial_key(api_key):
        return False, "not_trial_prefix"
    c = _conn()
    if c is None: return False, "no_database"
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT expires_at, signed_up_email FROM auto_trial_keys
                     WHERE api_key = %s
                """, (api_key,))
                r = cur.fetchone()
                if not r: return False, "unknown_trial_key"
                expires = r[0]
                if expires and expires < datetime.datetime.now(datetime.timezone.utc):
                    return False, "expired"
                # Touch last_used_at + call_count
                try:
                    cur.execute("""
                        UPDATE auto_trial_keys
                           SET last_used_at = NOW(),
                               call_count = call_count + 1
                         WHERE api_key = %s
                    """, (api_key,))
                except Exception: pass
                return True, "ok"
            except Exception:
                return False, "validation_failed"
    finally:
        try: c.close()
        except Exception: pass


@auto_trial_bp.route("/api/v1/keys/auto-mint", methods=["POST"])
def auto_mint_endpoint():
    """Direct callable for testing or alt clients. Same as the
    inline mint in mcp_gatekeeper."""
    tool = (request.args.get("tool") or "").strip()
    return jsonify(mint_trial_for_request(request, tool)), 200


@auto_trial_bp.route("/api/v1/keys/auto-trial/redeem", methods=["POST"])
def redeem_endpoint():
    """Bind a trial key to an email — converts the trial into a
    permanent IDENTIFIED-tier account. One-click conversion path."""
    d = request.get_json(silent=True) or {}
    api_key = (d.get("api_key") or "").strip()
    email   = (d.get("email") or "").strip().lower()
    if not is_trial_key(api_key):
        return jsonify(error="not_a_trial_key"), 400
    if "@" not in email or len(email) > 200:
        return jsonify(error="valid_email_required"), 400
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                UPDATE auto_trial_keys
                   SET signed_up_email = %s,
                       expires_at = NOW() + INTERVAL '365 days'
                 WHERE api_key = %s
                   AND (signed_up_email IS NULL OR signed_up_email = %s)
                RETURNING expires_at
            """, (email, api_key, email))
            r = cur.fetchone()
            if not r:
                return jsonify(error="key_not_found_or_already_bound"), 404
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, api_key=api_key, email=email,
                   tier="IDENTIFIED", daily_calls=200,
                   expires_at=r[0].isoformat() if r[0] else None,
                   message=(f"Trial key bound to {email}. You now have "
                            f"IDENTIFIED tier (200 calls/day) for 365 days. "
                            f"To upgrade to DEVELOPER ($49/mo, 2,000 calls/day): "
                            f"https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c"
                            f"?prefilled_email={email}")), 200


@auto_trial_bp.route("/api/v1/keys/auto-trial/stats", methods=["GET"])
def stats_endpoint():
    """Public funnel metrics for the auto-trial flow."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out = {
        "trials_minted_total":    0,
        "trials_minted_7d":       0,
        "trials_signed_up":       0,
        "signed_up_rate_pct":     0.0,
        "trials_upgraded":        0,
        "upgrade_rate_pct":       0.0,
        "active_unique_callers_7d": 0,
    }
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE minted_at >= NOW() - INTERVAL '7 days') AS minted_7d,
                           COUNT(*) FILTER (WHERE signed_up_email IS NOT NULL) AS signed_up,
                           COUNT(*) FILTER (WHERE upgraded_tier IS NOT NULL) AS upgraded,
                           COUNT(DISTINCT request_ip_hash) FILTER (WHERE minted_at >= NOW() - INTERVAL '7 days') AS callers_7d
                      FROM auto_trial_keys
                """)
                r = cur.fetchone() or (0, 0, 0, 0, 0)
                total, m7d, su, up, callers = (int(r[0] or 0), int(r[1] or 0),
                                                int(r[2] or 0), int(r[3] or 0),
                                                int(r[4] or 0))
                out["trials_minted_total"]    = total
                out["trials_minted_7d"]       = m7d
                out["trials_signed_up"]       = su
                out["trials_upgraded"]        = up
                out["active_unique_callers_7d"] = callers
                out["signed_up_rate_pct"] = round(100.0 * su / max(1, total), 2)
                out["upgrade_rate_pct"]   = round(100.0 * up / max(1, total), 2)
            except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    out["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
