"""Phase DD+ (2026-05-12) — MCP conversion plays 3-6.

Phase DD shipped plays 1+2 (pair-code magic link + funnel diagnostics).
This module bundles the remaining four lower-friction conversion paths
the user asked to ship together:

  Play 3: One-time top-up ($5 / 50 calls) — RETIRED 2026-09-11
          No new top-up is minted: POST /api/v1/mcp/topup/start answers
          410 and points at the one-time pack. Tokens minted before that
          still redeem, and a paid top-up's credits never expire.

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
  POST /api/v1/mcp/topup/start             — Play 3: 410, retired (-> the pack)
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

# ★2026-09-11 — THE $5 / 50-CALL TOP-UP (Play 3) IS RETIRED. Owner decision.
# Nothing sold it: no page, paywall or MCP response ever linked
# /api/v1/mcp/topup/start, and the human link it handed out,
# dchub.cloud/topup/<token>, never reached the worker — /topup/* is not in the
# frontend's _routes.json, so the edge answered 404. The endpoint was still
# minting tokens, and its checkout link fell back to the Developer SUBSCRIPTION
# link whenever DCHUB_STRIPE_TOPUP_LINK was unset (production's value was never
# read). The link, that fallback, their env knobs and the token minter are
# removed, so no variable can put it back on sale. Tokens already minted still
# redeem — see redeem_topup_token, which is also where a paid top-up stops
# expiring.

# r-pack5 (2026-06-16): the $5 / 1,000-credit one-time PACK — the cheap
# front-end acquisition offer (distinct from the legacy 50-credit top-up above).
# A fixed $5.00 one-time Stripe Payment Link created in the dashboard. Unlike the
# top-up (tu-token, keyed agent; retired 2026-09-11), the pack is a one-click,
# session-bound acquisition SKU: the $5 checkout mints a durable key + grants
# 1000 credits keyed on BOTH the key AND the buying mcp session, so the
# current session unlocks instantly (by session id) and future sessions unlock by
# the emailed durable key. Both burn the same balance (consume_credits, value-
# tiered cost). Reuses the proven mcp_topups storage — NOT a 2nd credits system.
PACK5_URL = os.environ.get(
    'DCHUB_PACK5_URL', 'https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i').strip()
PACK5_CREDITS = int(os.environ.get('DCHUB_PACK5_CREDITS', '1000'))
PACK5_PRICE_CENTS = int(os.environ.get('DCHUB_PACK5_PRICE_CENTS', '500'))

# r-pack10 (2026-06-25): a 2nd one-time credit pack — the repurposed ex-metered
# link ($10 one-time = 1,000 API calls, price_1TmOic…). Separate env knobs so
# the two packs can diverge in price/credits without code changes.
PACK10_CREDITS = int(os.environ.get('DCHUB_PACK10_CREDITS', '1000'))
PACK10_PRICE_CENTS = int(os.environ.get('DCHUB_PACK10_PRICE_CENTS', '1000'))

# ★2026-09-11 — PACK CREDITS NEVER EXPIRE. /pricing has said so since
# 2026-06-17, in three places (the pack Offer JSON-LD, the price note and the
# FAQ), one day after r-pack5 shipped a 90-day clock that no page, email or MCP
# message ever mentioned. Owner decision 2026-09-10: the code follows the page.
# The DCHUB_PACK5_EXPIRY_DAYS / DCHUB_PACK10_EXPIRY_DAYS knobs are GONE, not
# re-defaulted: an env var that can quietly re-arm the clock is how the promise
# broke, and what production had set could not even be read.
#
# "Never" is a far-future instant, not NULL and not 'infinity':
#   · mcp_topups.expires_at is NOT NULL (the legacy tu- top-ups use its
#     30-minute default). A NULL fails the INSERT, and grant_credit_pack
#     swallows that error — the buyer pays and receives no credits.
#   · 'infinity' has no Python datetime. Measured on Postgres 18: psycopg 3
#     raises DataError loading it, psycopg2 silently turns it into
#     datetime.max — and these rows are read back into Python (topup_status
#     calls .isoformat() on expires_at). This instant loads in both drivers,
#     in any session time zone.
# get_credit_balance, get_credit_status and consume_credits all filter on
# `expires_at > NOW()`, which this instant always passes.
PACK_NEVER_EXPIRES = "9999-12-31 00:00:00+00"

# Every `source` grant_credit_pack writes, and the only rows
# restore_pack_never_expires() may touch. Legacy tu- top-ups carry source NULL.
PACK_SOURCES = ("pack5", "pack10", "pack5_keybound", "pack10_keybound", "agentic_pack5")


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
    -- DEFAULT = an UNPAID tu- token's 30-minute checkout window, never a
    -- credit lifetime: payment writes PACK_NEVER_EXPIRES (redeem_topup_token,
    -- grant_credit_pack). 2026-09-11.
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

-- r-pack5 (2026-06-16): the $5/1000 pack keys credits on the buying mcp session
-- too (same-session instant unlock, no fragile tier-flip) + source tag.
ALTER TABLE mcp_topups ADD COLUMN IF NOT EXISTS mcp_session_id TEXT;
ALTER TABLE mcp_topups ADD COLUMN IF NOT EXISTS source TEXT;
CREATE INDEX IF NOT EXISTS mcp_topups_session_idx
    ON mcp_topups(mcp_session_id, paid_at DESC)
    WHERE mcp_session_id IS NOT NULL AND paid_at IS NOT NULL AND credits_remaining > 0;

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
# Play 3: one-time top-up ($5 / 50 calls) — RETIRED 2026-09-11: redeem only
# ═══════════════════════════════════════════════════════════════════════════

def _pack_offer() -> dict:
    """What the retired top-up points at instead: the one-time pack, read on
       every call and never frozen into copy. PACK5_URL (DCHUB_PACK5_URL) is the
       one-time pack link; PACK10_CREDITS / PACK10_PRICE_CENTS are what the
       webhook's pack branch grants and verifies for it."""
    cents = PACK10_PRICE_CENTS
    return {
        "pack_url": PACK5_URL,
        "credits": PACK10_CREDITS,
        "price_usd": cents / 100,
        "price_label": f"${cents // 100:,}" if cents % 100 == 0 else f"${cents / 100:,.2f}",
    }


@conversion_bp.post("/api/v1/mcp/topup/start")
def topup_start():
    """RETIRED 2026-09-11 — mints no token and opens no database connection.

       This used to hand an agent a tu- token and a checkout link for 50 calls
       at $5 (see the retirement note at the top of this module). The route
       stays registered so a caller that still knows it gets 410 Gone, a
       reason and the pack instead of a bare 404. Tokens minted before the
       retirement still redeem through the Stripe webhook."""
    offer = _pack_offer()
    return jsonify(
        ok=False,
        error="topup_retired",
        message=(f"The one-time top-up is retired. The one-time pack replaces it: "
                 f"{offer['credits']:,} API calls for {offer['price_label']}, "
                 f"no subscription — {offer['pack_url']}"),
        **offer,
    ), 410


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
    """User-facing page for a legacy top-up token (Play 3, retired 2026-09-11).

       Paid -> the confirmation, which no longer says "today": a paid top-up's
       credits never expire (redeem_topup_token). Unpaid -> 410 Gone: the
       top-up is not sold any more, so the page offers the one-time pack
       instead of the token's checkout. Unknown token -> 404."""
    from flask import Response
    from html import escape as _h
    token = token.strip()
    c = _conn()
    if c is None:
        return Response("<h1>Database unavailable</h1>", mimetype="text/html"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT credits, paid_at
                           FROM mcp_topups WHERE topup_token = %s""", (token,))
            row = cur.fetchone()
    finally:
        try: c.close()
        except Exception: pass
    if not row:
        return Response(f"<h1>Top-up token <code>{_h(token)}</code> not found</h1>",
                        mimetype="text/html"), 404
    credits, paid_at = row
    if paid_at:
        return Response(
            f"<!DOCTYPE html><meta charset=utf-8><title>Top-up complete</title>"
            f"<body style='font-family:system-ui;background:#0a0a12;color:#fff;"
            f"display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0'>"
            f"<div style='max-width:480px;text-align:center;padding:40px'>"
            f"<div style='font-size:3rem;color:#10b981'>✓</div>"
            f"<h1>Top-up complete</h1>"
            f"<p style='color:#9ca3af'>Your agent has {_h(str(credits))} extra calls. Tell it to retry — "
            f"the next call goes through.</p></div></body>",
            mimetype="text/html"), 200
    offer = _pack_offer()
    pack_url = _h(offer["pack_url"])
    price = _h(offer["price_label"])
    calls = _h(f"{offer['credits']:,}")
    return Response(
        f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<title>Top-up retired · DC Hub</title>
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
<div class="kicker">One-time top-up · retired</div>
<h1>This top-up is no longer sold</h1>
<p>The one-time pack replaces it. No subscription.</p>
<div class="price">{price}</div>
<div style="color:#9ca3af;font-size:.95rem">{calls} API calls · one-time</div>
<a href="{pack_url}" class="cta">Get {calls} calls — {price} →</a>
<div class="foot"><a href="/pricing">Compare plans →</a></div>
</div></body></html>""",
        mimetype="text/html"), 410


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
       starting with 'tu-'. Idempotent.

       ★2026-09-11 — PAYMENT WRITES expires_at = PACK_NEVER_EXPIRES. topup_start
       never set expires_at, so every tu- row carried the column DEFAULT, NOW()
       + 30 minutes — the unpaid token's checkout window — and this UPDATE set
       only paid_at. get_credit_balance, get_credit_status and consume_credits
       all require expires_at > NOW(), so the credits a buyer had just paid for
       left the balance 30 minutes after checkout STARTED, and a payment that
       completed later than that was worth nothing on arrival. Measured on
       Postgres 18.6 with this module's DDL: balance 50 at payment, 0 at +31
       minutes, while /api/v1/mcp/topup/<token>/status still reported paid with
       50 remaining. Owner decision: a paid top-up follows the pack rule. A
       webhook retry writes the same instant and keeps the first paid_at and
       session id."""
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
                    stripe_session_id = COALESCE(stripe_session_id, %s),
                    expires_at = %s::timestamptz
                WHERE topup_token = %s
                RETURNING id, credits;
            """, (stripe_session_id, PACK_NEVER_EXPIRES, token))
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
# r-pack5 (2026-06-16): $5 / 1,000-credit prepaid PACK — grant + balance + burn
# Reuses the mcp_topups storage + decrement shape; keys on api_key OR mcp session.
# ═══════════════════════════════════════════════════════════════════════════
def grant_credit_pack(api_key, mcp_session_id, credits,
                      stripe_session_id=None, source="pack5",
                      api_key_hash=None, price_cents=None):
    """Grant a one-time credit pack. IDEMPOTENT on stripe_session_id so a Stripe
       webhook retry never double-grants. Keys the balance on BOTH the durable
       api_key AND the buying mcp session (same-session instant unlock +
       durable-key reuse). Returns {ok, credits_granted, topup_id, idempotent}.
       Pass api_key_hash (a precomputed _hash_key(key) = sha256[:32]) to credit a
       key by its HASH without the raw key — the move-#3 key-bound $5 pack path,
       where the 'pk-<hash>' Stripe ref carries only the hash (raw key never
       reaches Stripe). get_credit_balance hashes the caller's key the same way,
       so the balance is found.

       ★2026-09-10 — price_cents IS A PARAMETER NOW. It was hardcoded to
       PACK5_PRICE_CENTS while `credits` and `expires_days` were already
       per-pack, so every $10 pack10 sale wrote 500 into mcp_topups.price_cents
       and revenue read back at HALF what was charged. Defaults to
       PACK5_PRICE_CENTS so the agentic $5 caller in routes/stripe_metered.py
       is unchanged.

       ★2026-09-11 — AND EXPIRY IS NOT. Every pack is written with
       expires_at = PACK_NEVER_EXPIRES, the promise /pricing makes; no argument
       and no env var is left that can shorten it."""
    out = {"ok": False}
    h = api_key_hash or _hash_key(api_key)
    if not h:
        out["error"] = "missing_api_key"; return out
    sid = (mcp_session_id or "").strip()[:200] or None
    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    try:
        with c, c.cursor() as cur:
            if stripe_session_id:
                cur.execute("SELECT id, credits FROM mcp_topups "
                            "WHERE stripe_session_id = %s LIMIT 1",
                            (stripe_session_id,))
                ex = cur.fetchone()
                if ex:
                    out.update(ok=True, idempotent=True,
                               topup_id=ex[0], credits_granted=ex[1])
                    return out
            token = f"{source}-" + _secrets.token_hex(8)
            cur.execute("""
                INSERT INTO mcp_topups
                    (topup_token, api_key_hash, credits, price_cents, paid_at,
                     expires_at, credits_remaining, stripe_session_id,
                     mcp_session_id, source)
                VALUES (%s, %s, %s, %s, NOW(),
                        %s::timestamptz, %s, %s, %s, %s)
                RETURNING id;
            """, (token, h, credits,
                  int(PACK5_PRICE_CENTS if price_cents is None else price_cents),
                  PACK_NEVER_EXPIRES,
                  credits, stripe_session_id, sid, source))
            row = cur.fetchone()
            out.update(ok=True, idempotent=False,
                       topup_id=row[0], credits_granted=credits)
            return out
    except Exception as e:
        out["error"] = str(e)[:200]
        print(f"[mcp_conversion_plays] grant_credit_pack: {e}", file=sys.stderr)
        return out
    finally:
        try: c.close()
        except Exception: pass


def restore_pack_never_expires():
    """Bring every pack row written under the retired 90-day clock onto the
       promise: expires_at = PACK_NEVER_EXPIRES for each PACK_SOURCES row still
       carrying an earlier instant. That includes a grant whose clock already
       ran out, so its unspent credits come back. Rows with source NULL — the
       legacy tu- top-ups — are never touched.

       IDEMPOTENT: once applied it matches nothing, so it runs on every import
       (below) with no flag and no ledger. The CTE is named `prior`, not `old`:
       Postgres 18 gives RETURNING its own OLD alias. Returns {ok, restored,
       earliest_old_expiry, latest_old_expiry}; never raises."""
    out = {"ok": False, "restored": 0}
    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                WITH prior AS (
                    SELECT id, expires_at FROM mcp_topups
                     WHERE source = ANY(%s)
                       AND expires_at < %s::timestamptz
                     FOR UPDATE
                )
                UPDATE mcp_topups AS t
                   SET expires_at = %s::timestamptz
                  FROM prior
                 WHERE t.id = prior.id
                RETURNING t.id, prior.expires_at;
            """, (list(PACK_SOURCES), PACK_NEVER_EXPIRES, PACK_NEVER_EXPIRES))
            moved = sorted((r[0], r[1]) for r in cur.fetchall() if r and r[1] is not None)
        olds = [old for _id, old in moved]
        out.update(ok=True, restored=len(moved),
                   earliest_old_expiry=min(olds).isoformat() if olds else None,
                   latest_old_expiry=max(olds).isoformat() if olds else None)
        if moved:
            # The audit trail is the log line itself: every row id with the
            # expiry it held, so the backfill can be reversed row by row.
            print(f"[mcp_conversion_plays] restore_pack_never_expires: {len(moved)} pack "
                  f"grant(s) moved to never-expires (old expiries "
                  f"{out['earliest_old_expiry']} .. {out['latest_old_expiry']}); prior "
                  "values: " + ", ".join(f"id={i}:{old.isoformat()}" for i, old in moved),
                  file=sys.stderr)
        return out
    except Exception as e:
        out["error"] = str(e)[:200]
        print(f"[mcp_conversion_plays] restore_pack_never_expires: {e}", file=sys.stderr)
        return out
    finally:
        try: c.close()
        except Exception: pass


# Every process import, once the schema exists. Idempotent — see above.
try:
    _PACK_EXPIRY_RESTORE = (restore_pack_never_expires() if _SCHEMA_OK
                            else {"ok": False, "restored": 0, "skipped": "schema_not_ready"})
except Exception:
    _PACK_EXPIRY_RESTORE = {"ok": False, "restored": 0, "skipped": "error"}


def get_credit_balance(api_key, mcp_session_id):
    """Total remaining credits for a caller, matched by durable key OR the buying
       session. Excludes expired/unpaid grants. Returns 0 on any failure
       (fail-soft → the gateway falls back to the free-taste path)."""
    h = _hash_key(api_key) if api_key else None
    sid = (mcp_session_id or "").strip()[:200] or None
    if not h and not sid:
        return 0
    c = _conn()
    if c is None:
        return 0
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(credits_remaining), 0)
                FROM mcp_topups
                WHERE paid_at IS NOT NULL
                  AND credits_remaining > 0
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND ((%s IS NOT NULL AND api_key_hash = %s)
                       OR (%s IS NOT NULL AND mcp_session_id = %s));
            """, (h, h, sid, sid))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] else 0
    except Exception as e:
        print(f"[mcp_conversion_plays] get_credit_balance: {e}", file=sys.stderr)
        return 0
    finally:
        try: c.close()
        except Exception: pass


def get_credit_status(api_key, mcp_session_id):
    """Like get_credit_balance but ALSO returns had_pack — whether the caller EVER
       bought a pack (even if now depleted/expired). Lets the gateway show a 'top up
       for 1,000 more' re-up nudge to a PROVEN buyer (highest-ROI re-conversion)
       instead of the generic claim-free-key teaser, and exempts a depleted buyer
       from the metered wall. Returns {'credits': int, 'had_pack': bool};
       fail-soft to {0, False}.

       ★2026-09-11 — had_pack WAS `source LIKE 'pack5%'`, written when pack5 was
       the only SKU. grant_credit_pack also writes pack10, pack10_keybound and
       agentic_pack5, so a buyer of the live $10 pack read had_pack=False the
       moment their credits ran out. It is membership in PACK_SOURCES now — the
       set tests/test_pack_credits_never_expire.py pins to every caller's source=.
       Bound as a list: psycopg2 sends a tuple as a record, which ANY() rejects."""
    h = _hash_key(api_key) if api_key else None
    sid = (mcp_session_id or "").strip()[:200] or None
    if not h and not sid:
        return {"credits": 0, "had_pack": False}
    c = _conn()
    if c is None:
        return {"credits": 0, "had_pack": False}
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                SELECT
                  COALESCE(SUM(credits_remaining) FILTER (
                      WHERE credits_remaining > 0
                        AND (expires_at IS NULL OR expires_at > NOW())), 0),
                  bool_or(source = ANY(%s))
                FROM mcp_topups
                WHERE paid_at IS NOT NULL
                  AND ((%s IS NOT NULL AND api_key_hash = %s)
                       OR (%s IS NOT NULL AND mcp_session_id = %s));
            """, (list(PACK_SOURCES), h, h, sid, sid))
            row = cur.fetchone()
            return {"credits": int(row[0]) if row and row[0] else 0,
                    "had_pack": bool(row[1]) if row else False}
    except Exception as e:
        print(f"[mcp_conversion_plays] get_credit_status: {e}", file=sys.stderr)
        return {"credits": 0, "had_pack": False}
    finally:
        try: c.close()
        except Exception: pass


def consume_credits(api_key, mcp_session_id, count=1):
    """Atomically burn `count` credits from the caller's most-recently-paid active
       grant (matched by durable key OR buying session). Refuses below zero.
       Returns {ok, remaining, burned}. Same decrement-with-RETURNING shape as
       consume_topup_credit, extended to also match the session key."""
    out = {"ok": False, "remaining": 0}
    h = _hash_key(api_key) if api_key else None
    sid = (mcp_session_id or "").strip()[:200] or None
    count = max(1, int(count or 1))
    if not h and not sid:
        out["error"] = "no_identity"; return out
    c = _conn()
    if c is None:
        out["error"] = "no_database"; return out
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                UPDATE mcp_topups
                SET credits_remaining = credits_remaining - %s
                WHERE id = (
                    SELECT id FROM mcp_topups
                    WHERE paid_at IS NOT NULL
                      AND credits_remaining >= %s
                      AND (expires_at IS NULL OR expires_at > NOW())
                      AND ((%s IS NOT NULL AND api_key_hash = %s)
                           OR (%s IS NOT NULL AND mcp_session_id = %s))
                    ORDER BY paid_at DESC LIMIT 1
                )
                RETURNING credits_remaining;
            """, (count, count, h, h, sid, sid))
            row = cur.fetchone()
            if row is None:
                out.update(ok=False, error="insufficient_credits", remaining=0)
                return out
            out.update(ok=True, remaining=int(row[0]), burned=count)
            return out
    except Exception as e:
        out["error"] = str(e)[:200]
        print(f"[mcp_conversion_plays] consume_credits: {e}", file=sys.stderr)
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
            (500 calls/day, all 7 ISO grid intel, fiber, M&A pipeline, energy):</p>
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
