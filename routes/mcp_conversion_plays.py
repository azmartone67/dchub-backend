"""Phase DD+ (2026-05-12) — MCP conversion plays 3-6.

Phase DD shipped plays 1+2 (pair-code magic link + funnel diagnostics).
This module bundles the remaining four lower-friction conversion paths
the user asked to ship together:

  Play 3: One-time top-up ($5 / 50 calls)
          For users hesitant about a $49/mo subscription but who need
          "just a few more queries for this project." Smaller commitment
          = higher conversion rate on hesitant users.

  Play 4: Per-tool demo unlock
          Currently paid tools return 403 with ZERO data. We now also
          ship one anonymized preview row when possible — agent quotes
          a concrete data point to its user → activates curiosity.
          Implementation: `demo_row_for(tool_name, market)` helper that
          composes tool-specific samples. Optional/opt-in; doesn't
          change the 403 status (back-compat).

  Play 5: Email-gated 7-day trial
          Capture emails of repeat paywall-hitters. Issue a 7-day
          Developer-tier trial key. Nurture via the autonomous press
          release engine (Phase BB). Long-tail conversion.

  Play 6: Per-agent affiliate attribution
          Capture which AI agent (Claude Desktop, Cursor, Gemini CLI,
          etc.) is referring upgrades. Show on /redeem page as a trust
          signal. New /agent-leaderboard endpoint surfaces "this week
          in AI adoption" data the marketing engine can quote.

Tables created idempotently on first import:
  mcp_topups            (Play 3)
  mcp_trial_emails      (Play 5)
  (Play 6 extends mcp_pair_codes with a referring_agent column)

Endpoints
---------
  POST /api/v1/mcp/topup/start             — Play 3: agent buys 50 calls
  GET  /api/v1/mcp/topup/<id>/status       — Play 3: agent polls
  POST /api/v1/trial/start                  — Play 5: email → trial key
  POST /api/v1/trial/<token>/redeem         — Play 5: magic-link consumer
  GET  /api/v1/mcp/agent-leaderboard        — Play 6: referrer ranking
"""
from __future__ import annotations
import os
import sys
import json
import re
import hashlib
import secrets as _secrets
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, request

conversion_bp = Blueprint("mcp_conversion_plays", __name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
RESEND_API_KEY = os.environ.get("DCHUB_RESEND_API_KEY", "")
ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
STRIPE_DEVELOPER_LINK = (
    os.environ.get('DCHUB_STRIPE_DEVELOPER_LINK')
    or 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c'
)
# Phase DD+: one-time $5 top-up Stripe Payment Link. Create in Stripe
# dashboard as a fixed $5.00 one-time price (NOT a subscription).
# Set env var DCHUB_STRIPE_TOPUP_LINK once configured. Fallback to the
# developer link if absent — payment still tracks, just upgrades to
# Developer plan instead of crediting calls.
STRIPE_TOPUP_LINK = os.environ.get('DCHUB_STRIPE_TOPUP_LINK',
                                    STRIPE_DEVELOPER_LINK).strip()
TOPUP_CREDITS = int(os.environ.get('DCHUB_TOPUP_CREDITS', '50'))
TOPUP_PRICE_CENTS = int(os.environ.get('DCHUB_TOPUP_PRICE_CENTS', '500'))


def _conn():
    if not DATABASE_URL: return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=8)
    except Exception as e:
        print(f"[mcp_conversion_plays] connect failed: {e}", file=sys.stderr)
        return None


def _hash_key(k: str) -> str:
    return hashlib.sha256((k or "").encode()).hexdigest()[:32]


_SCHEMA_DDL = """
-- Play 3: top-ups (one-time $5 = N call credits)
CREATE TABLE IF NOT EXISTS mcp_topups (
    id              BIGSERIAL PRIMARY KEY,
    topup_token     TEXT NOT NULL UNIQUE,            -- 'tu-XXXX' identifier in Stripe client_reference_id
    api_key_hash    TEXT NOT NULL,                   -- which agent
    credits         INTEGER NOT NULL DEFAULT 50,
    price_cents     INTEGER NOT NULL DEFAULT 500,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 minutes'),
    paid_at         TIMESTAMPTZ,
    stripe_session_id TEXT,
    credits_remaining INTEGER,                       -- decrement as calls land
    referring_agent TEXT
);
CREATE INDEX IF NOT EXISTS mcp_topups_token_idx ON mcp_topups(topup_token);
CREATE INDEX IF NOT EXISTS mcp_topups_active_idx
    ON mcp_topups(api_key_hash, paid_at DESC)
    WHERE paid_at IS NOT NULL AND credits_remaining > 0;

-- Play 5: email-gated 7-day trial captures
CREATE TABLE IF NOT EXISTS mcp_trial_emails (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT NOT NULL,
    magic_token     TEXT NOT NULL UNIQUE,           -- one-time magic link token
    trial_api_key   TEXT,                           -- issued AFTER they click the link
    source          TEXT,                            -- 'redeem_page' | 'pricing' | 'mcp_response'
    referring_agent TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    magic_clicked_at TIMESTAMPTZ,
    trial_started_at TIMESTAMPTZ,
    trial_expires_at TIMESTAMPTZ,
    converted_to_paid_at TIMESTAMPTZ,
    UNIQUE (email, source)
);
CREATE INDEX IF NOT EXISTS mcp_trial_emails_token_idx ON mcp_trial_emails(magic_token);
CREATE INDEX IF NOT EXISTS mcp_trial_active_idx
    ON mcp_trial_emails(trial_expires_at DESC)
    WHERE trial_expires_at IS NOT NULL
      AND converted_to_paid_at IS NULL;

-- Play 6: affiliate attribution on pair codes. Extends Phase DD's table.
-- ALTER TABLE IF EXISTS pattern is awkward in CREATE script; we use
-- conditional ADD COLUMN via DO block.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'mcp_pair_codes')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'mcp_pair_codes'
                         AND column_name = 'referring_agent') THEN
        ALTER TABLE mcp_pair_codes ADD COLUMN referring_agent TEXT;
    END IF;
END $$;
"""


def init_schema() -> bool:
    c = _conn()
    if c is None: return False
    try:
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[mcp_conversion_plays] init_schema failed: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


try:
    _SCHEMA_OK = init_schema()
except Exception:
    _SCHEMA_OK = False


# ═══════════════════════════════════════════════════════════════════════════
# Play 3: one-time top-up ($5 / 50 calls)
# ═══════════════════════════════════════════════════════════════════════════

def _new_topup_token() -> str:
    return "tu-" + _secrets.token_urlsafe(8).rstrip("=").replace("_", "").replace("-", "")[:10]


@conversion_bp.post("/api/v1/mcp/topup/start")
def topup_start():
    """Agent calls this when its human wants a one-time top-up. Returns
       a Stripe URL with client_reference_id=<token>. Stripe webhook
       reads the token, marks paid_at, and the agent's next calls
       consume from credits_remaining before hitting the daily cap.

       Cheaper, lower-commitment than a Developer subscription — for
       agents who just need to finish "this one project."
    """
    api_key = (request.headers.get("X-API-Key")
               or (request.json.get("api_key") if request.is_json else None)
               or request.args.get("api_key") or "")
    if not api_key:
        return jsonify(ok=False, error="api_key_required"), 400
    body = request.get_json(silent=True) or {}
    referring_agent = _capture_agent(body)

    c = _conn()
    if c is None: return jsonify(ok=False, error="no_database"), 503
    try:
        token = _new_topup_token()
        h = _hash_key(api_key)
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_topups
                    (topup_token, api_key_hash, credits, price_cents,
                     credits_remaining, referring_agent)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (topup_token) DO NOTHING
                RETURNING id, expires_at;
            """, (token, h, TOPUP_CREDITS, TOPUP_PRICE_CENTS,
                  TOPUP_CREDITS, referring_agent))
            row = cur.fetchone()
        c.commit()
        if not row:
            return jsonify(ok=False, error="token_collision_retry"), 503
        topup_id, expires_at = row
        stripe_url = (f"{STRIPE_TOPUP_LINK}"
                      f"{'&' if '?' in STRIPE_TOPUP_LINK else '?'}"
                      f"client_reference_id={token}")
        return jsonify(
            ok=True,
            topup_token=token,
            credits=TOPUP_CREDITS,
            price_usd=TOPUP_PRICE_CENTS / 100.0,
            stripe_url=stripe_url,
            expires_at=expires_at.isoformat(),
            redeem_url=f"https://dchub.cloud/topup/{token}",
            human_message=(
                f"💸 **One-time top-up: {TOPUP_CREDITS} extra calls for ${TOPUP_PRICE_CENTS/100:.2f}.** "
                f"No subscription. Tell your human to visit: "
                f"https://dchub.cloud/topup/{token} — one click, done."
            ),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@conversion_bp.get("/api/v1/mcp/topup/<token>/status")
def topup_status(token):
    """Agent polls this. Returns `paid: true` + `credits_remaining`
       once webhook fires."""
    c = _conn()
    if c is None: return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT paid_at, credits, credits_remaining, expires_at
                FROM mcp_topups WHERE topup_token = %s
            """, (token,))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="token_not_found"), 404
        paid_at, credits, remaining, expires_at = row
        return jsonify(
            ok=True,
            paid=bool(paid_at),
            paid_at=paid_at.isoformat() if paid_at else None,
            credits=credits,
            credits_remaining=remaining,
            expires_at=expires_at.isoformat() if expires_at else None,
        ), 200
    finally:
        try: c.close()
        except Exception: pass


@conversion_bp.get("/topup/<token>")
def topup_landing(token):
    """User-facing top-up page. Same UX pattern as /redeem/<code> but for
       the one-time $5 / 50-call offer."""
    from flask import Response
    from html import escape as _h
    token = token.strip()
    c = _conn()
    if c is None:
        return Response("<h1>Database unavailable</h1>", mimetype="text/html"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT credits, price_cents, paid_at, expires_at
                           FROM mcp_topups WHERE topup_token = %s""", (token,))
            row = cur.fetchone()
    finally:
        try: c.close()
        except Exception: pass
    if not row:
        return Response(f"<h1>Top-up token <code>{_h(token)}</code> not found</h1>",
                        mimetype="text/html"), 404
    credits, price_cents, paid_at, expires_at = row
    if paid_at:
        return Response(
            f"<!DOCTYPE html><meta charset=utf-8><title>Top-up complete</title>"
            f"<body style='font-family:system-ui;background:#0a0a12;color:#fff;"
            f"display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0'>"
            f"<div style='max-width:480px;text-align:center;padding:40px'>"
            f"<div style='font-size:3rem;color:#10b981'>✓</div>"
            f"<h1>Top-up complete</h1>"
            f"<p style='color:#9ca3af'>Your agent has {_h(credits)} extra calls today. Tell it to retry — "
            f"the next call goes through.</p></div></body>",
            mimetype="text/html"), 200
    stripe_url = (f"{STRIPE_TOPUP_LINK}"
                  f"{'&' if '?' in STRIPE_TOPUP_LINK else '?'}"
                  f"client_reference_id={_h(token)}")
    return Response(
        f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Top-up · DC Hub</title>
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>body{{font-family:'Instrument Sans',system-ui;background:#0a0a0f;color:#fff;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px}}
.wrap{{max-width:520px;background:#11121a;border:1px solid #1f2030;border-radius:14px;padding:36px}}
.kicker{{font-size:.72rem;letter-spacing:.14em;color:#10b981;text-transform:uppercase;font-weight:700;margin-bottom:10px;font-family:JetBrains Mono,monospace}}
h1{{margin:0 0 10px;letter-spacing:-.02em}}p{{color:#9ca3af}}
.price{{font-size:2.4rem;font-weight:800;margin:18px 0 4px}}
.cta{{display:block;background:linear-gradient(135deg,#10b981,#6366f1);color:#fff;text-align:center;padding:16px;border-radius:10px;text-decoration:none;font-weight:700;margin-top:24px}}
a{{color:#6366f1}}.foot{{margin-top:18px;text-align:center;font-size:.78rem}}
</style></head><body><div class="wrap">
<div class="kicker">💸 ONE-TIME TOP-UP</div>
<h1>Extra calls — no subscription</h1>
<p>Your AI agent needs more queries today. Skip the $49/mo commitment with a one-time credit pack.</p>
<div class="price">${_h(f"{price_cents/100:.2f}")}</div>
<div style="color:#9ca3af;font-size:.95rem">{_h(credits)} additional calls · valid for the rest of today</div>
<a href="{stripe_url}" class="cta">Pay ${_h(f"{price_cents/100:.2f}")} — Unlock now →</a>
<div class="foot"><a href="/pricing">Want unlimited? Compare plans →</a></div>
</div></body></html>""",
        mimetype="text/html"), 200


def consume_topup_credit(api_key: str, count: int = 1) -> bool:
    """Called from the rate-limit middleware after a successful paid call.
       Returns True if a credit was consumed (meaning the call should be
       allowed beyond the free-tier cap). The caller still gates by
       plan — this is purely a fallback for free-tier users who topped up.
    """
    if not api_key:
        return False
    c = _conn()
    if c is None: return False
    h = _hash_key(api_key)
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                UPDATE mcp_topups
                SET credits_remaining = credits_remaining - %s
                WHERE id = (
                    SELECT id FROM mcp_topups
                    WHERE api_key_hash = %s
                      AND paid_at IS NOT NULL
                      AND credits_remaining >= %s
                    ORDER BY paid_at DESC LIMIT 1
                )
                RETURNING credits_remaining;
            """, (count, h, count))
            return cur.fetchone() is not None
    except Exception as e:
        print(f"[mcp_conversion_plays] consume_topup_credit: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


def redeem_topup_token(token: str, stripe_session_id: str | None = None) -> dict:
    """Stripe webhook calls this when checkout completes for a token
       starting with 'tu-'. Idempotent."""
    out = {"ok": False, "token": token}
    if not token:
        out["error"] = "missing_token"
        return out
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                UPDATE mcp_topups
                SET paid_at = COALESCE(paid_at, NOW()),
                    stripe_session_id = COALESCE(stripe_session_id, %s)
                WHERE topup_token = %s
                RETURNING id, credits;
            """, (stripe_session_id, token))
            row = cur.fetchone()
        if not row:
            out["error"] = "token_not_found"
            return out
        out["ok"] = True
        out["topup_id"] = row[0]
        out["credits"] = row[1]
        return out
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    finally:
        try: c.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════
# Play 4: per-tool demo unlock
# ═══════════════════════════════════════════════════════════════════════════
# Used by build_paywall_response() in utils/paywall_response.py. Returns
# ONE plausible anonymized row for the requested tool, so the AI agent
# can quote a single concrete data point to its user as proof-of-value.
#
# These are NOT live data — they're carefully constructed samples that
# match the shape of the real response. Each row carries a `_demo: true`
# field so callers / UIs can render them differently.

_DEMO_ROWS = {
    "get_grid_intelligence": {
        "iso": "MISO", "lmp_$/mwh": 38.20, "load_mw": 89_400,
        "renewable_mix_pct": 28.5, "headroom_mw": 4_220,
        "_demo": True,
        "_note": "Demo row — upgrade to Developer for live data across 7 ISOs"
    },
    "get_fiber_intel": {
        "metro": "Northern Virginia",
        "long_haul_routes": 47, "ix_presence": "Equinix DC1, DC2",
        "top_carriers": ["Lumen", "Crown Castle", "Zayo"],
        "_demo": True,
        "_note": "Demo row — upgrade for full carrier graph + dark fiber"
    },
    "get_facility": {
        "name": "Demo Facility · Sample Campus", "city": "Ashburn",
        "state": "VA", "country": "US", "power_mw": "—",
        "_demo": True,
        "_note": "Demo row — upgrade for capacity, coordinates, provider"
    },
    "get_market_intel": {
        "market": "Northern Virginia",
        "verdict": "BUILD", "excess_power_score": 72.4,
        "constraint_score": 38.0,
        "_demo": True,
        "_note": "Demo row — full 280+ market intelligence on Developer"
    },
    "get_water_risk": {
        "basin": "Sample HUC8", "stress_score": 2.4,
        "_demo": True,
        "_note": "Demo row — full WRI + EPA + state allocation data on Developer"
    },
    "get_energy_prices": {
        "state": "VA", "industrial_cents_kwh": 8.4,
        "ytd_change_pct": 2.1,
        "_demo": True,
        "_note": "Demo row — full state-by-state historical pricing on Developer"
    },
    "get_renewable_energy": {
        "state": "TX", "wind_mw": 39_421, "solar_mw": 18_770,
        "_demo": True,
        "_note": "Demo row — full ISO renewable mix + curtailment on Developer"
    },
    "analyze_site": {
        "score": 72,
        "_demo": True,
        "_note": "Demo score — full site analysis (grid, fiber, water, tax) on Developer"
    },
}


def demo_row_for(tool_name: str | None) -> dict | None:
    """Return one safe demo row for a tool, or None if no demo is defined.
       Called from build_paywall_response when the caller doesn't provide
       a real trial_preview_data slice."""
    if not tool_name: return None
    return _DEMO_ROWS.get(tool_name)


# ═══════════════════════════════════════════════════════════════════════════
# Play 5: email-gated 7-day trial
# ═══════════════════════════════════════════════════════════════════════════

def _new_magic_token() -> str:
    return "tr-" + _secrets.token_urlsafe(16).rstrip("=")


def _capture_agent(body: dict) -> str | None:
    """Best-effort: extract the calling AI agent identity for affiliate
       attribution. MCP clients vary in how they self-identify."""
    explicit = body.get("client_name") or body.get("referring_agent")
    if explicit:
        return str(explicit)[:80]
    ua = request.headers.get("X-Client-Name") or request.headers.get("User-Agent") or ""
    ua_low = ua.lower()
    for known in ("claude", "cursor", "gpt", "openai", "gemini",
                   "perplexity", "cline", "windsurf", "copilot", "grok"):
        if known in ua_low:
            return known
    return (ua[:60] or None)


@conversion_bp.post("/api/v1/trial/start")
def trial_start():
    """Capture an email, send a magic link, queue a 7-day Developer trial.
       Honeypot-protected. Captures source + referring_agent for funnel
       attribution.
    """
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    honeypot = body.get("website") or body.get("hp")  # bot check
    if honeypot:
        return jsonify(ok=False, error="rejected"), 400
    # Tight email validation
    if not re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email):
        return jsonify(ok=False, error="invalid_email"), 400
    # Reject internal / generic / disposable
    bad_domains = ("dchub.cloud", "example.com", "test.com",
                   "mailinator.com", "tempmail.com")
    if any(email.endswith("@" + d) for d in bad_domains):
        return jsonify(ok=False, error="email_disallowed"), 400
    source = body.get("source", "trial_form")[:40]

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        token = _new_magic_token()
        referring_agent = _capture_agent(body)
        with c, c.cursor() as cur:
            # ON CONFLICT (email, source) so re-submitting the same
            # email from the same source replays the existing token
            # rather than spamming.
            cur.execute("""
                INSERT INTO mcp_trial_emails
                    (email, magic_token, source, referring_agent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (email, source) DO UPDATE SET
                    magic_token     = EXCLUDED.magic_token,
                    referring_agent = EXCLUDED.referring_agent
                RETURNING id, magic_token;
            """, (email, token, source, referring_agent))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="db_insert_failed"), 500
        magic_url = f"https://dchub.cloud/trial/{row[1]}"
        _send_trial_email(email, magic_url)
        return jsonify(
            ok=True,
            email_sent=bool(RESEND_API_KEY),
            magic_url_preview=magic_url if not RESEND_API_KEY else None,
            note=("Check your inbox for the magic link."
                  if RESEND_API_KEY
                  else "DCHUB_RESEND_API_KEY not configured — magic link returned inline (dev mode only)."),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


def _send_trial_email(email: str, magic_url: str) -> bool:
    """Best-effort Resend send. Returns True on success."""
    if not RESEND_API_KEY:
        print(f"[trial_start] DCHUB_RESEND_API_KEY not set; would send to {email}: {magic_url}",
              flush=True)
        return False
    from urllib.request import Request, urlopen
    payload = json.dumps({
        "from":    "DC Hub <noreply@dchub.cloud>",
        "to":      [email],
        "subject": "Your 7-day DC Hub Developer trial — one click to activate",
        "html":    f"""
            <h2>Activate your 7-day DC Hub Developer trial</h2>
            <p>Click the button below to activate a full Developer-tier API key
            (1,000 calls/day, all 7 ISO grid intel, fiber, M&A pipeline, energy):</p>
            <p><a href="{magic_url}" style="display:inline-block;background:#6366f1;
                  color:#fff;text-decoration:none;padding:14px 28px;border-radius:8px;
                  font-weight:700;font-family:system-ui">Activate trial →</a></p>
            <p style="color:#666;font-size:.85rem">Or copy this link: {magic_url}</p>
            <p style="color:#666;font-size:.85rem">The trial runs for 7 days from
            activation. After that, you can subscribe to Developer ($49/mo) or fall
            back to the free tier.</p>
            <p style="color:#999;font-size:.75rem;margin-top:30px">
            DC Hub · Data Center Intelligence Platform · <a href="https://dchub.cloud">dchub.cloud</a>
            </p>
        """,
    }).encode()
    req = Request("https://api.resend.com/emails", data=payload, headers={
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {RESEND_API_KEY}",
    })
    try:
        with urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"[trial_start] Resend send failed: {e}", file=sys.stderr)
        return False


@conversion_bp.get("/api/v1/trial/<token>/redeem")
def trial_redeem(token):
    """User clicks the magic link → we mint a Developer-tier trial key
       and return it. The key expires after 7 days (a daily cron job
       can revoke; until that exists, the gating layer checks
       trial_expires_at against NOW())."""
    c = _conn()
    if c is None: return jsonify(ok=False, error="no_database"), 503
    try:
        # Mark clicked + provision key, idempotently
        trial_key = "dchub_trial_" + _secrets.token_urlsafe(24).rstrip("=")
        with c.cursor() as cur:
            cur.execute("""
                UPDATE mcp_trial_emails
                SET magic_clicked_at   = COALESCE(magic_clicked_at, NOW()),
                    trial_api_key      = COALESCE(trial_api_key, %s),
                    trial_started_at   = COALESCE(trial_started_at, NOW()),
                    trial_expires_at   = COALESCE(trial_expires_at,
                                                  NOW() + INTERVAL '7 days')
                WHERE magic_token = %s
                RETURNING email, trial_api_key, trial_expires_at;
            """, (trial_key, token))
            row = cur.fetchone()
        c.commit()
        if not row:
            return jsonify(ok=False, error="invalid_token"), 404
        email, key, expires_at = row
        return jsonify(
            ok=True,
            email=email,
            trial_api_key=key,
            trial_expires_at=expires_at.isoformat() if expires_at else None,
            note="Trial active. Use this key as X-API-Key for 7 days of Developer access.",
            upgrade_url="https://dchub.cloud/pricing",
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════
# Play 6: per-agent affiliate attribution
# ═══════════════════════════════════════════════════════════════════════════

@conversion_bp.get("/api/v1/mcp/agent-leaderboard")
def agent_leaderboard():
    """Which AI agent has sent the most successful upgrades?

       Sources:
         - mcp_pair_codes.referring_agent + redeemed_at
         - mcp_topups.referring_agent + paid_at
         - mcp_trial_emails.referring_agent + magic_clicked_at

       This is the autonomous-marketing-engine's "this week in AI
       adoption" content. Pulls into /dc-hub-media as a leaderboard
       and Phase BB's daily press release can quote it verbatim.
    """
    try:
        window_days = int(request.args.get("days", "7"))
    except ValueError:
        window_days = 7
    window_days = max(1, min(window_days, 90))

    c = _conn()
    if c is None: return jsonify(error="no_database", items=[]), 503
    try:
        with c.cursor() as cur:
            cur.execute(f"""
                WITH all_referrals AS (
                    SELECT referring_agent, 'pair_code_redeemed' AS event, redeemed_at AS t
                    FROM mcp_pair_codes
                    WHERE redeemed_at > NOW() - INTERVAL '{window_days} days'
                      AND referring_agent IS NOT NULL
                    UNION ALL
                    SELECT referring_agent, 'topup_paid' AS event, paid_at AS t
                    FROM mcp_topups
                    WHERE paid_at > NOW() - INTERVAL '{window_days} days'
                      AND referring_agent IS NOT NULL
                    UNION ALL
                    SELECT referring_agent, 'trial_started' AS event, trial_started_at AS t
                    FROM mcp_trial_emails
                    WHERE trial_started_at > NOW() - INTERVAL '{window_days} days'
                      AND referring_agent IS NOT NULL
                )
                SELECT referring_agent,
                       COUNT(*) AS total_referrals,
                       SUM(CASE WHEN event='pair_code_redeemed' THEN 1 ELSE 0 END) AS conversions,
                       SUM(CASE WHEN event='topup_paid'         THEN 1 ELSE 0 END) AS topups,
                       SUM(CASE WHEN event='trial_started'      THEN 1 ELSE 0 END) AS trials,
                       MAX(t) AS most_recent
                FROM all_referrals
                GROUP BY referring_agent
                ORDER BY total_referrals DESC
                LIMIT 30
            """)
            rows = cur.fetchall()
        items = [{
            "agent": r[0],
            "total_referrals": int(r[1] or 0),
            "conversions":     int(r[2] or 0),
            "topups":          int(r[3] or 0),
            "trials":          int(r[4] or 0),
            "most_recent":     r[5].isoformat() if r[5] else None,
        } for r in rows]
        resp = jsonify(
            as_of=datetime.now(timezone.utc).isoformat(),
            window_days=window_days,
            count=len(items),
            items=items,
            citation=("DC Hub MCP affiliate leaderboard. "
                      "https://dchub.cloud/api/v1/mcp/agent-leaderboard"),
        )
        resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        return resp, 200
    except Exception as e:
        return jsonify(error=str(e)[:200], items=[]), 500
    finally:
        try: c.close()
        except Exception: pass
