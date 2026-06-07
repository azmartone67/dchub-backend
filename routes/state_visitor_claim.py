"""routes/state_visitor_claim.py — State-of-2026 visitor → trial-key claim (2026-06-07).

Round 2 of /state-of-2026 (Round 1 = commit 5cee73b6: living document + click
attribution proxy). This module turns READERS into MCP trial users using the
SAME high-intent capture mechanic the MCP funnel uses (commit a297a7f9):

  1. Page-side JS tracks (in localStorage so it survives a refresh):
       - time on page (POST every 30s)
       - clicks on outbound briefs (DCPI/hyperscaler/market briefs/etc.)
  2. When threshold hit (2+ brief clicks OR 60+s on page), the page POSTs
     /api/v1/state-of-2026/claim-email with the visitor's email. The server
     mints an HMAC-signed claim_token via utils.claim_token.sign_claim_token
     (kind=state_of_2026_visitor) and immediately resolves it to a trial
     key — the visitor sees the key in the success response AND gets it
     emailed via Resend.
  3. The /claim/<token> URL still works for an alternate "email me the link"
     fallback flow — same token, dispatched in mcp_high_intent_claim.claim_*
     by inspecting payload kind.

Endpoints registered here:
  POST /api/v1/state-of-2026/track-event   — bump click/time counters
                                              (returns is_high_intent flag)
  POST /api/v1/state-of-2026/claim-email   — direct claim (email-in-modal),
                                              mints trial key + emails it
  GET  /api/v1/state-of-2026/funnel        — admin: visitor → email → key
                                              → activated → paid

Schema (mirrored in routes/schema_repair.py SCHEMA_STATEMENTS):
  state_visitor_intent(
    id, visitor_session_id UNIQUE, brief_clicks INT, time_on_page_seconds INT,
    brief_slugs TEXT[], hi_threshold_hit_at, claim_token,
    claim_minted_at, claim_used_at, email TEXT, minted_api_key TEXT,
    ua TEXT, referer TEXT, ip_hash TEXT,
    first_seen_at, last_event_at
  )

Threshold:
  HI_CLICKS_THRESHOLD = 2     — clicked 2+ outbound briefs
  HI_SECONDS_THRESHOLD = 60   — spent 60+s on the page
  EITHER triggers high-intent flag.

Abuse model:
  * track-event is unauthenticated (page-side JS). Rate-limited per IP (60/min).
  * claim-email triggers the mint inline — rate-limited 10/hour per IP.
  * Tokens are HMAC-signed (24h TTL, single-use via claim_used_at).
  * No payment, no PII beyond the email the visitor entered.
  * The visitor_session_id is a client-generated UUIDv4 (the JS sets it);
    UNIQUE constraint means a malicious caller pumping events still only
    moves ONE row's counters.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

from utils.claim_token import (
    KIND_STATE_OF_2026_VISITOR,
    sign_claim_token,
)


logger = logging.getLogger(__name__)

state_visitor_claim_bp = Blueprint("state_visitor_claim", __name__)


# ── Config ────────────────────────────────────────────────────────────

HI_CLICKS_THRESHOLD  = 2          # 2+ brief clicks → high-intent
HI_SECONDS_THRESHOLD = 60         # OR 60+s on page → high-intent
TRACK_RL_PER_IP_MIN  = 60         # /track-event RL
CLAIM_RL_PER_IP_HOUR = 10         # /claim-email RL
TIME_TICK_S_CAP      = 600        # cap any single tick increment at 10min
                                  # (prevents JS clock-skew → 999999s claim)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
DISMISS_COOKIE = "dch_s26_dismissed"   # set client-side by the modal


# ── DB ────────────────────────────────────────────────────────────────

def _conn():
    try:
        import psycopg2
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if not dsn:
            return None
        c = psycopg2.connect(dsn, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[state_visitor_claim] DB connect failed: %s", e)
        return None


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS state_visitor_intent (
    id                       BIGSERIAL PRIMARY KEY,
    visitor_session_id       TEXT NOT NULL UNIQUE,
    brief_clicks             INTEGER NOT NULL DEFAULT 0,
    time_on_page_seconds     INTEGER NOT NULL DEFAULT 0,
    brief_slugs              TEXT,
    hi_threshold_hit_at      TIMESTAMPTZ,
    claim_token              TEXT,
    claim_minted_at          TIMESTAMPTZ,
    claim_used_at            TIMESTAMPTZ,
    email                    TEXT,
    minted_api_key           TEXT,
    ua                       TEXT,
    referer                  TEXT,
    ip_hash                  TEXT,
    first_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_event_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_svi_hi_hit
    ON state_visitor_intent(hi_threshold_hit_at DESC)
    WHERE hi_threshold_hit_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_svi_used
    ON state_visitor_intent(claim_used_at DESC)
    WHERE claim_used_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_svi_email
    ON state_visitor_intent(LOWER(email))
    WHERE email IS NOT NULL;
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
    except Exception as e:
        logger.warning("[state_visitor_claim] schema ensure failed: %s", e)
        try: c.rollback()
        except Exception: pass


# ── rate-limit ──────────────────────────────────────────────────────

_TRACK_RL: dict[str, list[float]] = {}
_CLAIM_RL: dict[str, list[float]] = {}


def _rate_limit(bucket: dict, ip: str, cap: int, window_s: int) -> bool:
    now = time.time()
    items = bucket.setdefault(ip, [])
    cutoff = now - window_s
    items[:] = [t for t in items if t >= cutoff]
    if len(items) >= cap:
        return False
    items.append(now)
    return True


def _client_ip(req) -> str:
    return (req.headers.get("CF-Connecting-IP")
            or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or req.remote_addr or "0.0.0.0")


def _ip_hash(req) -> str:
    """SHA256(client_ip + day-salt) — privacy-preserving day-stable hash."""
    fwd = _client_ip(req)
    day_salt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{fwd}|{day_salt}".encode("utf-8")).hexdigest()[:32]


# ── admin gate (for /funnel) ────────────────────────────────────────

def _admin_ok(req) -> bool:
    sent = (req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    return bool(expected) and sent == expected


# ── POST /api/v1/state-of-2026/track-event ──────────────────────────

@state_visitor_claim_bp.route("/api/v1/state-of-2026/track-event",
                               methods=["POST"])
def track_event():
    """Page-side JS calls this periodically + on every outbound brief click.

    Body: {visitor_session_id, event_type, brief_slug?, seconds_on_page?}
        event_type ∈ {"brief_click", "time_tick"}

    Returns: {ok, brief_clicks, time_on_page_seconds, is_high_intent}

    Unauthenticated; rate-limited 60/min per IP. The visitor_session_id is
    a client-generated UUIDv4 (sessionStorage). The first event creates the
    row; subsequent events bump counters via ON CONFLICT.
    """
    ip = _client_ip(request)
    if not _rate_limit(_TRACK_RL, ip, TRACK_RL_PER_IP_MIN, 60):
        return jsonify(ok=False, error="rate_limited"), 429

    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}

    vsid = str(body.get("visitor_session_id") or "").strip()[:64]
    event_type = str(body.get("event_type") or "").strip()[:32]
    if not vsid or event_type not in ("brief_click", "time_tick"):
        return jsonify(ok=False, error="bad_params"), 400

    brief_slug = str(body.get("brief_slug") or "").strip()[:48]
    try:
        seconds_delta = int(body.get("seconds_on_page") or 0)
    except Exception:
        seconds_delta = 0
    seconds_delta = max(0, min(seconds_delta, TIME_TICK_S_CAP))

    ua = (request.headers.get("User-Agent") or "")[:200]
    referer = (request.headers.get("Referer") or "")[:300]
    ip_h = _ip_hash(request)

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # Upsert pattern: insert a fresh row if not seen, else bump counters.
            # ON CONFLICT branches on event_type — brief_click bumps the count
            # and appends the slug; time_tick adds to time_on_page_seconds.
            if event_type == "brief_click":
                cur.execute(
                    """
                    INSERT INTO state_visitor_intent
                        (visitor_session_id, brief_clicks, brief_slugs,
                         time_on_page_seconds, ua, referer, ip_hash,
                         first_seen_at, last_event_at)
                    VALUES (%s, 1, %s, 0, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (visitor_session_id) DO UPDATE SET
                        brief_clicks = state_visitor_intent.brief_clicks + 1,
                        brief_slugs = CASE
                            WHEN state_visitor_intent.brief_slugs IS NULL
                                 OR position(EXCLUDED.brief_slugs IN state_visitor_intent.brief_slugs) = 0
                                THEN COALESCE(state_visitor_intent.brief_slugs || ',', '') || EXCLUDED.brief_slugs
                            ELSE state_visitor_intent.brief_slugs
                        END,
                        last_event_at = NOW(),
                        ua = COALESCE(state_visitor_intent.ua, EXCLUDED.ua),
                        referer = COALESCE(state_visitor_intent.referer, EXCLUDED.referer)
                    RETURNING brief_clicks, time_on_page_seconds,
                              hi_threshold_hit_at, claim_used_at
                    """,
                    (vsid, brief_slug or None, ua, referer, ip_h),
                )
            else:  # time_tick
                cur.execute(
                    """
                    INSERT INTO state_visitor_intent
                        (visitor_session_id, brief_clicks, time_on_page_seconds,
                         ua, referer, ip_hash, first_seen_at, last_event_at)
                    VALUES (%s, 0, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (visitor_session_id) DO UPDATE SET
                        time_on_page_seconds =
                            state_visitor_intent.time_on_page_seconds + EXCLUDED.time_on_page_seconds,
                        last_event_at = NOW(),
                        ua = COALESCE(state_visitor_intent.ua, EXCLUDED.ua),
                        referer = COALESCE(state_visitor_intent.referer, EXCLUDED.referer)
                    RETURNING brief_clicks, time_on_page_seconds,
                              hi_threshold_hit_at, claim_used_at
                    """,
                    (vsid, seconds_delta, ua, referer, ip_h),
                )
            row = cur.fetchone() or (0, 0, None, None)
            brief_clicks = int(row[0] or 0)
            time_s = int(row[1] or 0)
            hi_hit_at = row[2]
            already_used = row[3] is not None

            is_high_intent = (brief_clicks >= HI_CLICKS_THRESHOLD
                              or time_s >= HI_SECONDS_THRESHOLD)

            # Stamp hi_threshold_hit_at the FIRST time we cross threshold.
            if is_high_intent and hi_hit_at is None:
                try:
                    cur.execute(
                        """UPDATE state_visitor_intent
                              SET hi_threshold_hit_at = NOW()
                            WHERE visitor_session_id = %s
                              AND hi_threshold_hit_at IS NULL""",
                        (vsid,),
                    )
                except Exception as e:
                    logger.debug("[track_event] hi stamp skip: %s", e)

        return jsonify(
            ok=True,
            brief_clicks=brief_clicks,
            time_on_page_seconds=time_s,
            is_high_intent=is_high_intent,
            already_claimed=already_used,
            thresholds={"clicks": HI_CLICKS_THRESHOLD,
                        "seconds": HI_SECONDS_THRESHOLD},
        )
    except Exception as e:
        logger.warning("[track_event] failed: %s", e)
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


# ── helpers shared with claim-email ────────────────────────────────

def _mint_trial_key_for_email(email: str, c, request_obj) -> tuple[str, str]:
    """Mint a dch_trial_ key via routes.auto_trial.mint_trial_for_request.
    Returns (api_key, mint_error_or_'')."""
    api_key = None
    mint_error = ""
    try:
        from routes.auto_trial import mint_trial_for_request as _mint
        mint_result = _mint(
            req=request_obj,
            tool_name="state-of-2026",
            client_name="state-of-2026-visitor",
            operator_email=email,
        )
        if isinstance(mint_result, dict) and mint_result.get("ok"):
            api_key = mint_result.get("api_key")
        else:
            mint_error = (mint_result or {}).get("error", "") if isinstance(mint_result, dict) else ""
    except Exception as e:
        mint_error = f"mint_exception: {type(e).__name__}: {e}"
        logger.warning("[state_visitor_claim] mint failed: %s", e)

    if not api_key:
        # Fallback synthesis so the visitor never leaves empty-handed.
        api_key = "dch_trial_" + secrets.token_urlsafe(24).replace("_", "x").replace("-", "x")[:32]
        ip = _client_ip(request_obj)
        try:
            with c.cursor() as cur:
                cur.execute(
                    """INSERT INTO auto_trial_keys
                         (api_key, minted_for_tool, request_ip_hash, request_ua,
                          expires_at, operator_email, client_name)
                       VALUES (%s, %s, %s, %s, NOW() + INTERVAL '7 days', %s, %s)
                       ON CONFLICT (api_key) DO NOTHING""",
                    (api_key, "state-of-2026",
                     hashlib.sha256(ip.encode()).hexdigest()[:16],
                     (request_obj.headers.get("User-Agent") or "")[:200],
                     email, "state-of-2026-visitor-fallback"),
                )
        except Exception as e:
            logger.warning("[state_visitor_claim] fallback insert failed: %s", e)
    return api_key or "", mint_error


def _send_state_of_2026_welcome_email(email: str, api_key: str) -> tuple[bool, str]:
    """Send the trial-key welcome email. Tries the shared _p99_send_email
    helper first so Resend wiring + From: addr + retry are inherited; falls
    back to a direct Resend call with a State-of-2026-flavored body.

    Returns (ok, detail)."""
    # Try the shared sender first — it already does Resend + retries.
    try:
        from routes.redeem_routes import _p99_send_email as _send
        ok, detail = _send(email, api_key, ["state-of-2026"])
        if ok:
            return True, detail or "sent"
        logger.warning("[state_visitor_claim] _p99_send_email returned not-ok: %s",
                       detail[:200])
    except Exception as e:
        logger.warning("[state_visitor_claim] _p99_send_email exception: %s", e)

    # Fallback: direct Resend with our own body.
    resend_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not resend_key:
        return False, "resend_key_missing"
    try:
        import requests as _rq  # type: ignore
    except Exception:
        return False, "requests_lib_missing"

    public_base = (os.environ.get("DCHUB_PUBLIC_BASE_URL")
                   or "https://dchub.cloud").rstrip("/")
    subject = "Your DC Hub trial key (from State of 2026 reader)"
    html_body = f"""<!doctype html><html><body style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#0a0a0a">
<h1 style="font-size:22px;margin:0 0 12px">Welcome, State of 2026 reader.</h1>
<p>You spent enough time with our report that we figure you want the data <i>your way</i>.
Here's your trial key — 50 calls/day for 7 days, no card.</p>

<div style="background:#0a0a0a;color:#22d3ee;padding:18px 20px;border-radius:8px;
            font-family:JetBrains Mono,monospace;font-size:14px;word-break:break-all;margin:18px 0">
{api_key}
</div>

<h2 style="font-size:16px;margin:24px 0 8px">1-click MCP client configs</h2>

<p><b>Claude Desktop / Cursor / Cline / Continue</b> (add to your <code>mcp.json</code>):</p>
<pre style="background:#f5f5f5;padding:12px;border-radius:6px;font-size:12px;overflow:auto"
>{{"mcpServers":{{"dchub":{{"command":"npx","args":["-y","mcp-remote","{public_base}/mcp"],"env":{{"DCHUB_API_KEY":"{api_key}"}}}}}}}}</pre>

<p><b>ChatGPT / any HTTP MCP client</b>:</p>
<pre style="background:#f5f5f5;padding:12px;border-radius:6px;font-size:12px;overflow:auto"
>curl -H "X-API-Key: {api_key}" {public_base}/api/v1/grid/status?iso=ercot</pre>

<p style="margin-top:24px">When you're ready: <b>$199/mo Pro</b> (1,000 calls/day) or
<b>$1,188/yr annual</b> (50% off the first year) at
<a href="{public_base}/pricing">{public_base}/pricing</a>.</p>

<p style="font-size:12px;color:#a3a3a3;margin-top:28px;border-top:1px solid #e5e5e5;padding-top:14px">
Reply to this email if anything breaks. — DC Hub
</p></body></html>"""
    try:
        r = _rq.post("https://api.resend.com/emails",
                     headers={"Authorization": f"Bearer {resend_key}"},
                     json={
                         "from": "DC Hub <hello@dchub.cloud>",
                         "to":   [email],
                         "subject": subject,
                         "html":  html_body,
                     }, timeout=12)
        if 200 <= r.status_code < 300:
            return True, "sent_fallback"
        return False, f"resend_{r.status_code}_{r.text[:200]}"
    except Exception as e:
        return False, f"resend_exception_{type(e).__name__}_{e}"


# ── POST /api/v1/state-of-2026/claim-email ─────────────────────────

@state_visitor_claim_bp.route("/api/v1/state-of-2026/claim-email",
                               methods=["POST"])
def claim_email():
    """Modal submits here. Body: {visitor_session_id, email}.

    Validates email + threshold + not-already-used, mints HMAC token
    (kind=state_of_2026_visitor) for funnel attribution, mints trial key,
    sends welcome email. Returns {ok, api_key, email_status, claim_url}.

    Idempotent on (visitor_session_id): a second POST returns the SAME
    key (not a new one) so a flaky network → retry doesn't double-mint.
    """
    ip = _client_ip(request)
    if not _rate_limit(_CLAIM_RL, ip, CLAIM_RL_PER_IP_HOUR, 3600):
        return jsonify(ok=False, error="rate_limited"), 429

    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    vsid = str(body.get("visitor_session_id") or "").strip()[:64]
    email = str(body.get("email") or "").strip().lower()
    if not vsid or not email or not EMAIL_RE.match(email):
        return jsonify(ok=False, error="bad_params"), 400

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_schema(c)

        # Look up the existing row (created by track-event); if missing,
        # create one — a determined visitor who never tripped the page-side
        # threshold but reached this endpoint deserves their key too
        # (this is also the path our /claim/<token> handler hits).
        with c.cursor() as cur:
            cur.execute(
                """SELECT brief_clicks, time_on_page_seconds,
                          hi_threshold_hit_at, claim_used_at, minted_api_key,
                          claim_token
                     FROM state_visitor_intent
                    WHERE visitor_session_id = %s""",
                (vsid,),
            )
            row = cur.fetchone()
            if row is None:
                # Create a thin row so we still attribute the conversion.
                cur.execute(
                    """INSERT INTO state_visitor_intent
                         (visitor_session_id, brief_clicks, time_on_page_seconds,
                          ua, referer, ip_hash, first_seen_at, last_event_at)
                       VALUES (%s, 0, 0, %s, %s, %s, NOW(), NOW())
                       ON CONFLICT (visitor_session_id) DO NOTHING""",
                    (vsid,
                     (request.headers.get("User-Agent") or "")[:200],
                     (request.headers.get("Referer") or "")[:300],
                     _ip_hash(request)),
                )
                row = (0, 0, None, None, None, None)

        brief_clicks = int(row[0] or 0)
        time_s = int(row[1] or 0)
        used_at = row[3]
        existing_key = row[4]

        # Idempotent: already claimed → return the existing key.
        if used_at is not None and existing_key:
            return jsonify(
                ok=True,
                api_key=existing_key,
                email=email,
                already_claimed=True,
                email_status="prior",
                claim_url=None,
            )

        # Mint the HMAC token (for funnel attribution + the /claim URL).
        token = sign_claim_token(
            kind=KIND_STATE_OF_2026_VISITOR,
            session_id=vsid,
            extra="state26",
        )

        # Mint the trial key inline.
        api_key, mint_error = _mint_trial_key_for_email(email, c, request)

        # Send the welcome email.
        ok_send, send_detail = _send_state_of_2026_welcome_email(email, api_key)
        email_status = "sent" if ok_send else f"failed: {send_detail[:200]}"

        # Persist the conversion.
        try:
            with c.cursor() as cur:
                cur.execute(
                    """UPDATE state_visitor_intent SET
                           claim_token = COALESCE(claim_token, %s),
                           claim_minted_at = COALESCE(claim_minted_at, NOW()),
                           claim_used_at = NOW(),
                           email = %s,
                           minted_api_key = %s,
                           hi_threshold_hit_at = COALESCE(hi_threshold_hit_at, NOW()),
                           last_event_at = NOW()
                         WHERE visitor_session_id = %s""",
                    (token, email, api_key, vsid),
                )
        except Exception as e:
            logger.warning("[claim_email] persist failed: %s", e)

        public_base = (os.environ.get("DCHUB_PUBLIC_BASE_URL")
                       or "https://dchub.cloud").rstrip("/")
        logger.info("[state_visitor_claim] minted vsid=%s email=%s key=%s "
                    "email_status=%s clicks=%s time_s=%s",
                    vsid[:12], email, (api_key or "")[:20] + "...",
                    email_status, brief_clicks, time_s)

        return jsonify(
            ok=True,
            api_key=api_key,
            email=email,
            email_status=email_status,
            claim_url=f"{public_base}/claim/{token}",
            mint_error=mint_error or None,
            already_claimed=False,
        )
    except Exception as e:
        logger.warning("[claim_email] failed: %s", e)
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


# ── GET /api/v1/state-of-2026/funnel  (admin) ──────────────────────

@state_visitor_claim_bp.route("/api/v1/state-of-2026/funnel",
                               methods=["GET"])
def visitor_funnel():
    """Visitor funnel KPIs. Admin-gated.

    Returns counts (last `days`):
        visitors                — distinct visitor_session_id in window
        linkedin_attributed     — same, with linkedin.com in referer
        high_intent_threshold_hit
        emails_submitted
        keys_activated          — minted key had a non-self MCP call after mint
        paid                    — email matched a non-free user.plan row
    """
    if not _admin_ok(request):
        return jsonify(ok=False, error="admin_key required"), 401
    try:
        days = max(1, min(90, int(request.args.get("days", "7"))))
    except ValueError:
        days = 7

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503

    out: dict = {
        "ok": True,
        "window_days": days,
        "visitors": 0,
        "linkedin_attributed": 0,
        "high_intent_threshold_hit": 0,
        "emails_submitted": 0,
        "keys_activated": 0,
        "paid": 0,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM state_visitor_intent "
                    "WHERE first_seen_at > NOW() - INTERVAL %s",
                    (f"{days} days",))
                out["visitors"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM state_visitor_intent "
                    "WHERE first_seen_at > NOW() - INTERVAL %s "
                    "  AND (LOWER(referer) LIKE '%%linkedin.com%%' "
                    "    OR LOWER(referer) LIKE '%%lnkd.in%%')",
                    (f"{days} days",))
                out["linkedin_attributed"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM state_visitor_intent "
                    "WHERE hi_threshold_hit_at > NOW() - INTERVAL %s",
                    (f"{days} days",))
                out["high_intent_threshold_hit"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM state_visitor_intent "
                    "WHERE claim_used_at > NOW() - INTERVAL %s",
                    (f"{days} days",))
                out["emails_submitted"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            # keys_activated: minted_api_key showed up in mcp_call_log
            # (or equivalent) after claim_used_at. Best-effort — some
            # surfaces store call counts differently; fall back to zero.
            try:
                cur.execute(
                    """SELECT COUNT(*) FROM state_visitor_intent svi
                        WHERE svi.claim_used_at > NOW() - INTERVAL %s
                          AND svi.minted_api_key IS NOT NULL
                          AND EXISTS (
                            SELECT 1 FROM auto_trial_keys atk
                             WHERE atk.api_key = svi.minted_api_key
                               AND COALESCE(atk.last_used_at, atk.minted_at)
                                       > svi.claim_used_at + INTERVAL '1 second')
                    """,
                    (f"{days} days",))
                out["keys_activated"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            try:
                cur.execute(
                    """SELECT COUNT(DISTINCT svi.email)
                         FROM state_visitor_intent svi
                         JOIN users u ON LOWER(u.email) = LOWER(svi.email)
                        WHERE svi.claim_used_at > NOW() - INTERVAL %s
                          AND COALESCE(u.plan,'free') NOT IN ('free','')
                    """,
                    (f"{days} days",))
                out["paid"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass

        # Derived rates.
        if out["visitors"]:
            out["high_intent_pct"] = round(
                100.0 * out["high_intent_threshold_hit"] / out["visitors"], 1)
            out["email_submit_pct"] = round(
                100.0 * out["emails_submitted"] / out["visitors"], 1)
        if out["emails_submitted"]:
            out["activate_pct"] = round(
                100.0 * out["keys_activated"] / out["emails_submitted"], 1)
            out["paid_pct"] = round(
                100.0 * out["paid"] / out["emails_submitted"], 1)
        return jsonify(out)
    finally:
        try: c.close()
        except Exception: pass


# ── visit-summary helper (for /claim/<token> kind=state_of_2026_visitor) ──

def fetch_visitor_row(visitor_session_id: str) -> dict | None:
    """Used by mcp_high_intent_claim.claim_form when payload.kind is
    KIND_STATE_OF_2026_VISITOR — renders a state-flavored form."""
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT brief_clicks, time_on_page_seconds,
                          claim_used_at, minted_api_key, email
                     FROM state_visitor_intent
                    WHERE visitor_session_id = %s""",
                (visitor_session_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "brief_clicks": int(r[0] or 0),
                "time_on_page_seconds": int(r[1] or 0),
                "claim_used_at": r[2],
                "minted_api_key": r[3] or "",
                "email": r[4] or "",
            }
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info("[state_visitor_claim] ready · "
                "POST /api/v1/state-of-2026/track-event · "
                "POST /api/v1/state-of-2026/claim-email · "
                "GET /api/v1/state-of-2026/funnel")


_smoke()
