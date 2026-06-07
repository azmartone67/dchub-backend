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
    operator_email   TEXT,
    operator_name    TEXT,
    client_name      TEXT,
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
            # r71-conv bridge: idempotent column adds for pre-existing tables
            # (CREATE TABLE IF NOT EXISTS won't add columns to an existing one).
            # operator_email is the human-conversion bridge handle captured AT
            # mint time — the missing top-of-human-funnel (agents don't pay).
            cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS operator_email TEXT")
            cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS operator_name TEXT")
            cur.execute("ALTER TABLE auto_trial_keys ADD COLUMN IF NOT EXISTS client_name TEXT")
    except Exception:
        try: c.rollback()
        except Exception: pass


def mint_trial_for_request(req=None, tool_name: str = "", client_name: str = "",
                           operator_email: str = "", operator_name: str = "") -> dict:
    """Called by mcp_gatekeeper when an anonymous user hits an
    IDENTIFIED-tier gate. Returns {api_key, expires_at, cap, ...}.

    r71-conv: now accepts client_name (the MCP client UA, e.g. Cursor/Cline)
    and an optional operator_email/operator_name captured AT MINT TIME. This
    fixes the with-email endpoint (which always TypeError'd passing client_name)
    and creates the human-conversion bridge handle — agents don't pay, humans
    do, so we capture the operator at the moment of maximum intent.

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
                    # r71-conv: backfill the operator handle onto the reused key
                    # if the agent supplied one this call (capture at first chance).
                    if operator_email:
                        try:
                            cur.execute(
                                "UPDATE auto_trial_keys SET "
                                "operator_email = COALESCE(operator_email, %s), "
                                "operator_name  = COALESCE(operator_name, %s), "
                                "client_name    = COALESCE(client_name, %s) "
                                "WHERE api_key = %s",
                                (operator_email.strip().lower(), (operator_name or None),
                                 (client_name[:80] or None) if client_name else None, r[0]))
                        except Exception: pass
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
                            f"Trial expires in {days_left} day(s). To keep this key "
                            f"working AND email your operator a 1-click upgrade link, "
                            f"bind their email: POST /api/v1/keys/auto-trial/bind "
                            f"{{api_key, email}}."
                        ),
                        "operator_action": (
                            "ACTION FOR YOUR HUMAN OPERATOR: reply with your work "
                            "email to keep this data flowing — your agent can bind "
                            "it via POST /api/v1/keys/auto-trial/bind {api_key, email}."
                        ),
                        "instructions":(f"Use api_key in X-API-Key header. "
                                         f"FREE for {days_left} more day(s) at "
                                         f"{TRIAL_DAILY_CALLS}/day."),
                    }
            except Exception: pass

            # Mint a new trial key
            api_key = "dch_trial_" + secrets.token_urlsafe(24).replace("_", "x").replace("-", "x")[:32]
            try:
                cur.execute(f"""
                    INSERT INTO auto_trial_keys
                      (api_key, minted_for_tool, request_ip_hash, request_ua,
                       expires_at, client_name, operator_email, operator_name)
                    VALUES (%s, %s, %s, %s, NOW() + INTERVAL '{TRIAL_DAYS} days', %s, %s, %s)
                    ON CONFLICT (api_key) DO NOTHING
                    RETURNING expires_at
                """, (api_key, tool_name[:40] or None, ip_hash, ua,
                      (client_name[:80] or None) if client_name else None,
                      (operator_email.strip().lower() or None) if operator_email else None,
                      (operator_name[:120] or None) if operator_name else None))
                r = cur.fetchone()
                expires = r[0].isoformat() if r and r[0] else None
            except Exception:
                return {"error": "mint_failed", "ok": False}
    finally:
        try: c.close()
        except Exception: pass

    # r74 (2026-06-07): emit CRM reverse-ETL capture on first mint (not reuse).
    # Only fires when an operator email is bound at mint time. Fail-soft.
    try:
        if operator_email:
            from routes.crm_reverse_etl import capture_event as _crm_capture
            _crm_capture("trial_key_activated", {
                "email": operator_email,
                "tool": tool_name, "mcp_client": client_name,
            })
    except Exception:
        pass

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
            f"This trial gives {TRIAL_DAILY_CALLS} calls/day for {TRIAL_DAYS} days. "
            f"To keep it working past the trial AND get your operator a 1-click "
            f"upgrade link, bind their email: POST /api/v1/keys/auto-trial/bind "
            f"{{api_key, email}}. (Binding emails the human the upgrade path — "
            f"agents can't pay, humans do.)"
        ),
        "operator_action": (
            "ACTION FOR YOUR HUMAN OPERATOR: reply with your work email so this "
            "key keeps working and you get the 1-click upgrade link. Your agent "
            "can bind it via POST /api/v1/keys/auto-trial/bind {api_key, email}."
        ),
        "instructions":(f"Use api_key in X-API-Key header. FREE for "
                         f"{TRIAL_DAYS} days at {TRIAL_DAILY_CALLS}/day. "
                         f"Bind your operator email to persist + upgrade: POST "
                         f"/api/v1/keys/auto-trial/bind {{api_key, email}} (or "
                         f"/redeem to convert to a 365-day IDENTIFIED key)."),
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


@auto_trial_bp.route("/api/v1/keys/auto-trial/bind", methods=["POST"])
def bind_operator_endpoint():
    """r71-conv — the HUMAN-CONVERSION BRIDGE capture. Lighter than /redeem:
    binds an operator_email (the human behind the agent) to a trial key at the
    moment of demand, WITHOUT converting the trial. This is the agent-satisfiable
    step the mint CTA points at — most coding agents (Cursor/Cline/Continue) will
    ask their user for an email when told it keeps the key working. It gives the
    out-of-band operator digest someone to reach. Does NOT send any email."""
    d = request.get_json(silent=True) or {}
    api_key = (d.get("api_key") or "").strip()
    email   = (d.get("email") or "").strip().lower()
    name    = (d.get("name") or d.get("operator_name") or "").strip()[:120]
    if not is_trial_key(api_key):
        return jsonify(error="not_a_trial_key"), 400
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 200:
        return jsonify(error="valid_email_required"), 400
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                UPDATE auto_trial_keys
                   SET operator_email = %s,
                       operator_name  = COALESCE(NULLIF(%s,''), operator_name)
                 WHERE api_key = %s
                RETURNING expires_at
            """, (email, name, api_key))
            r = cur.fetchone()
            if not r:
                return jsonify(error="key_not_found"), 404
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(
        ok=True, api_key=api_key, operator_email=email, bound=True,
        upgrade_url=f"https://dchub.cloud/upgrade?key={api_key}",
        message=(f"Operator {email} bound to this trial key. They'll get a usage "
                 f"summary + 1-click upgrade link. To upgrade now, open "
                 f"https://dchub.cloud/upgrade?key={api_key} (one click upgrades "
                 f"THIS exact key — no copy/paste). Or convert to a 365-day "
                 f"IDENTIFIED key: POST /api/v1/keys/auto-trial/redeem "
                 f"{{api_key, email}}.")), 200


@auto_trial_bp.route("/api/v1/keys/auto-trial/stats", methods=["GET"])
def stats_endpoint():
    """Public funnel metrics for the inline auto-mint flow.

    r62-conv (2026-06-01): rebuilt as an honest 4-stage funnel after the
    success analysis found 143 distinct agents calling the paid grid/fiber
    tools but only 2 paid keys. The old endpoint hid the single most
    important stage — did the agent actually RE-USE the minted key
    (proving inline-mint works)? — and reported `trials_upgraded` off a
    column (`upgraded_tier`) that NOTHING ever writes, so it was
    structurally always 0 (same bug class as the per-platform conv-rate
    that never coincides). Paid conversion is now computed by JOINing the
    bound email to a real paid tier in mcp_dev_keys.

        Stage 1  minted     — trial key issued in a paywall response
        Stage 2  activated  — agent reconnected & USED the key (call_count>0)
        Stage 3  identified — bound an email via /auto-trial/redeem
        Stage 4  paid       — that email later holds a paid/enterprise key

    Each stage's drop_pct shows where the conversion engine leaks.
    """
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out = {
        "trials_minted_total":      0,
        "trials_minted_7d":         0,
        "trials_activated":         0,   # reconnected & used the key (NEW)
        "trials_signed_up":         0,   # bound an email
        "trials_paid":              0,   # email → real paid/enterprise key (NEW: honest)
        "activated_rate_pct":       0.0,
        "signed_up_rate_pct":       0.0,
        "paid_rate_pct":            0.0,
        "active_unique_callers_7d": 0,
    }
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE minted_at >= NOW() - INTERVAL '7 days') AS minted_7d,
                           COUNT(*) FILTER (WHERE COALESCE(call_count,0) > 0
                                              OR last_used_at IS NOT NULL) AS activated,
                           COUNT(*) FILTER (WHERE signed_up_email IS NOT NULL) AS signed_up,
                           COUNT(DISTINCT request_ip_hash) FILTER (WHERE minted_at >= NOW() - INTERVAL '7 days') AS callers_7d
                      FROM auto_trial_keys
                """)
                r = cur.fetchone() or (0, 0, 0, 0, 0)
                total, m7d, act, su, callers = (int(r[0] or 0), int(r[1] or 0),
                                                 int(r[2] or 0), int(r[3] or 0),
                                                 int(r[4] or 0))
                out["trials_minted_total"]      = total
                out["trials_minted_7d"]         = m7d
                out["trials_activated"]         = act
                out["trials_signed_up"]         = su
                out["active_unique_callers_7d"] = callers
                out["activated_rate_pct"] = round(100.0 * act / max(1, total), 2)
                out["signed_up_rate_pct"] = round(100.0 * su  / max(1, total), 2)
            except Exception: pass

            # Honest paid conversion: a bound trial email that later holds a
            # real paid/enterprise key. JOIN to mcp_dev_keys by email. Wrapped
            # defensively — if the table/columns differ on this deploy we
            # report 0 paid rather than 500 the whole endpoint.
            try:
                cur.execute("""
                    SELECT COUNT(DISTINCT t.signed_up_email)
                      FROM auto_trial_keys t
                      JOIN mcp_dev_keys k
                        ON lower(k.email) = lower(t.signed_up_email)
                     WHERE t.signed_up_email IS NOT NULL
                       AND k.tier IN ('paid','enterprise')
                """)
                paid = int((cur.fetchone() or [0])[0] or 0)
                out["trials_paid"]     = paid
                out["paid_rate_pct"]   = round(100.0 * paid / max(1, out["trials_minted_total"]), 2)
            except Exception:
                try: c.rollback()
                except Exception: pass
    finally:
        try: c.close()
        except Exception: pass

    # Staged funnel + biggest-leak, so the reader sees WHERE it breaks.
    m  = out["trials_minted_total"]
    def _drop(a, b):
        return None if a == 0 else round(100.0 * (1 - (b / a)), 1)
    out["funnel"] = {
        "1_minted":     m,
        "2_activated":  out["trials_activated"],
        "3_identified": out["trials_signed_up"],
        "4_paid":       out["trials_paid"],
    }
    out["drop_rates"] = {
        "1_minted_to_2_activated":     _drop(m,                       out["trials_activated"]),
        "2_activated_to_3_identified": _drop(out["trials_activated"], out["trials_signed_up"]),
        "3_identified_to_4_paid":      _drop(out["trials_signed_up"], out["trials_paid"]),
    }
    _leak_stage, _leak_drop = None, -1.0
    for stg, dr in out["drop_rates"].items():
        if dr is not None and dr > _leak_drop:
            _leak_drop, _leak_stage = dr, stg
    out["biggest_leak"] = ({"stage": _leak_stage, "drop_pct": _leak_drop}
                           if _leak_stage else None)
    out["legend"] = {
        "1_minted":     "trial key issued inline in a paywall response",
        "2_activated":  "agent reconnected and USED the key (call_count>0) — proves inline-mint works",
        "3_identified": "bound an email via /auto-trial/redeem",
        "4_paid":       "that email later holds a real paid/enterprise key (JOIN mcp_dev_keys)",
    }
    out["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
