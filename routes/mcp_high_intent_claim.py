"""routes/mcp_high_intent_claim.py — Session-bound 3-strike one-click claim (2026-06-07).

The structural gap this closes: 132 distinct users hit get_grid_intelligence
(paid tool) in 30d, 131 hit get_fiber_intel, ZERO of them converted via the
agent-self-serve path. The paywall message goes to the agent (not the human),
LLM-rendered links rarely get clicked, and anonymous MCP sessions have no
identity to follow up with.

This module adds a HIGH-INTENT capture layer ON TOP of the existing paywall:
  * Track per-MCP-session_id paid-tool calls in the last 24h.
  * When count crosses 3 on a paid tool, mint a HMAC-signed claim_token.
  * The MCP server embeds the resulting https://dchub.cloud/claim/<token>
    URL in the paywall response — a SHORT, clear, single-action link the
    agent will relay verbatim to the human.
  * Human clicks the link → email-only form → trial key minted via
    auto_trial.mint_trial_for_request → emailed via Resend with the key +
    1-click MCP config snippet.

Why HMAC and not a DB-side token? The whole token verification path is
~80us synchronous — no DB roundtrip, no race with deploys, no Stripe-style
signed-by-Stripe complexity. The token IS the proof that the backend told
the MCP server "this session is high-intent right now." DB row exists for
funnel attribution + idempotency (claim_used_at), not for token validity.

Routes:
  POST /api/v1/mcp/track-paid-hit          — internal-keyed; called by mcp-server
  GET  /api/v1/mcp/should-mint-claim       — internal-keyed; returns claim_token or null
  GET  /api/v1/mcp/high-intent/stats       — public funnel KPIs (claim_rate_30d, claim_to_paid_30d)
  GET  /claim/<token>                       — public; renders 1-field email form
  POST /claim/<token>                       — public; mints trial key, sends email, marks used

Schema (added to schema_repair.SCHEMA_STATEMENTS so a /admin/schema/repair sweep
creates it):
  mcp_high_intent_sessions(
    id PK, mcp_session_id, tool_name, paid_call_count_24h,
    first_hit_at, last_hit_at, claim_token, claim_minted_at, claim_used_at,
    minted_api_key, claim_email, UNIQUE(mcp_session_id, tool_name)
  )

Signing:
  HMAC-SHA256(secret, mcp_session_id|tool_name|minted_ts).hexdigest()[:32]
  Token format: <base64url(payload)>.<sig>  where payload = sid:tool:ts
  secret = DCHUB_HMAC_SECRET env var, fallback to DCHUB_ADMIN_KEY[:32].

Abuse model:
  * Token is single-use (claim_used_at uniqueness).
  * 24h TTL on token (rejected on /claim/<token> if older).
  * Per-IP rate-limit on /claim POST (10/hour) — script can't farm-mint.
  * track-paid-hit requires X-Internal-Key — only mcp-server can bump count.
  * Even if someone leaks a token, all it does is mint a 7d/50call trial key
    in the email they choose. No payment, no escalation.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request


logger = logging.getLogger(__name__)
mcp_high_intent_claim_bp = Blueprint("mcp_high_intent_claim", __name__)


# ── Config ────────────────────────────────────────────────────────────

# Round 2 (2026-06-07): threshold env-driven so we can A/B without redeploys.
# Default dropped 3→2 because the funnel data showed enormous drop-off after
# the FIRST paid-tool hit on every UA (get_grid_intelligence: 4 distinct
# callers, all with count=1; compare_sites: 22 distinct callers, all 1-2).
# The "3 strikes" gate was discarding 95%+ of would-be high-intent sessions.
# Operator can pin back to 3 (conservative) via DCHUB_HIGH_INTENT_THRESHOLD=3.
HIGH_INTENT_THRESHOLD = int(os.environ.get("DCHUB_HIGH_INTENT_THRESHOLD", "2"))
CLAIM_TOKEN_TTL_S     = 24 * 3600    # 24h
CLAIM_RL_PER_IP_HOUR  = 10           # POST /claim rate-limit

# Round 2: per-platform claim copy A/B. The mcp-server sends ?variant=X
# alongside the track-paid-hit / should-mint-claim calls. We store the
# variant on the row so the variant-conversion endpoint can compute
# per-variant minted/used/paid rates.
VALID_VARIANTS = {"claude", "cursor", "cline", "chatgpt", "generic"}


def _norm_variant(v: str | None) -> str:
    """Normalize variant string to one of VALID_VARIANTS, else 'generic'."""
    s = (v or "").strip().lower()
    return s if s in VALID_VARIANTS else "generic"


def _hmac_secret() -> bytes:
    s = (os.environ.get("DCHUB_HMAC_SECRET") or "").strip()
    if not s:
        s = (os.environ.get("DCHUB_ADMIN_KEY") or "")[:32]
    if not s:
        # Last-resort dev fallback so a missing env doesn't 500 every call —
        # the dev secret is intentionally weak so this surfaces immediately
        # in any prod logging (operator sees "DCHUB_HMAC_SECRET unset").
        s = "dchub-dev-secret-set-DCHUB_HMAC_SECRET"
        logger.warning("[high_intent_claim] DCHUB_HMAC_SECRET unset — using dev fallback")
    return s.encode("utf-8")


def _internal_ok(req) -> bool:
    """X-Internal-Key OR X-Admin-Key OR ?admin_key= — same pattern as
    schema_repair._admin_ok. We accept both because the mcp-server uses
    X-Internal-Key; manual admin probing uses X-Admin-Key."""
    sent = (req.headers.get("X-Internal-Key")
            or req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    if not sent:
        return False
    expected_admin = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    expected_internal = (os.environ.get("DCHUB_INTERNAL_KEY")
                         or "").strip()
    return sent in (expected_admin, expected_internal) and bool(sent)


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
        logger.warning("[high_intent_claim] DB connect failed: %s", e)
        return None


# ── Token sign / verify ──────────────────────────────────────────────
# Round 2 (2026-06-07): delegate to utils.claim_token so state_visitor_claim
# uses the SAME secret + payload format. Legacy 3-field MCP tokens (kind
# implicit = mcp_session) still verify bit-for-bit. Module-level imports
# are at the top of the file.
from utils.claim_token import (
    KIND_MCP_SESSION,
    KIND_STATE_OF_2026_VISITOR,
    sign_claim_token as _shared_sign,
    verify_claim_token as _shared_verify,
)


def sign_claim_token(mcp_session_id: str, tool_name: str,
                     ts: int | None = None) -> str:
    """Sign an MCP-session claim token. Legacy 3-field payload — preserved
    for backward-compat. New kinds should use utils.claim_token directly."""
    return _shared_sign(
        kind=KIND_MCP_SESSION,
        session_id=mcp_session_id,
        extra=tool_name,
        ts=ts,
    )


def verify_claim_token(token: str) -> dict | None:
    """Returns {session_id, tool, ts} on success, None on any failure.

    Round 2 compatibility: works for BOTH legacy 3-field MCP tokens AND
    the new state-of-2026 visitor tokens. The caller can inspect `.kind`
    to branch; existing callers that only look at `session_id` + `tool`
    keep working unchanged because we map `extra` → `tool` here.
    """
    payload = _shared_verify(token)
    if not payload:
        return None
    return {
        "session_id": payload["session_id"],
        "tool":       payload["extra"],   # legacy field name
        "ts":         payload["ts"],
        "kind":       payload["kind"],    # new field; legacy callers ignore
    }


# ── Rate-limit (in-memory) for POST /claim ────────────────────────────

_RL_BUCKET: dict[str, list[float]] = {}


def _rate_limit_ok(ip: str) -> bool:
    now = time.time()
    bucket = _RL_BUCKET.setdefault(ip, [])
    # Keep only the last hour.
    cutoff = now - 3600
    bucket[:] = [t for t in bucket if t >= cutoff]
    if len(bucket) >= CLAIM_RL_PER_IP_HOUR:
        return False
    bucket.append(now)
    return True


def _client_ip(req) -> str:
    return (req.headers.get("CF-Connecting-IP")
            or req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or req.remote_addr or "0.0.0.0")


# ── Schema (also added to schema_repair.SCHEMA_STATEMENTS for the canonical sweep) ──

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mcp_high_intent_sessions (
    id                   BIGSERIAL PRIMARY KEY,
    mcp_session_id       TEXT NOT NULL,
    tool_name            TEXT NOT NULL,
    paid_call_count_24h  INTEGER NOT NULL DEFAULT 0,
    first_hit_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_hit_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claim_token          TEXT,
    claim_minted_at      TIMESTAMPTZ,
    claim_used_at        TIMESTAMPTZ,
    claim_email          TEXT,
    minted_api_key       TEXT,
    user_agent           TEXT,
    mcp_client           TEXT,
    claim_variant        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mhis_sid_tool
    ON mcp_high_intent_sessions(mcp_session_id, tool_name);
CREATE INDEX IF NOT EXISTS ix_mhis_minted_at
    ON mcp_high_intent_sessions(claim_minted_at DESC)
    WHERE claim_minted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_mhis_used_at
    ON mcp_high_intent_sessions(claim_used_at DESC)
    WHERE claim_used_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_mhis_token
    ON mcp_high_intent_sessions(claim_token)
    WHERE claim_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_mhis_variant
    ON mcp_high_intent_sessions(claim_variant)
    WHERE claim_variant IS NOT NULL;
-- Round 2 (2026-06-07): idempotent ALTER for rows that pre-date variant tracking.
-- ADD COLUMN IF NOT EXISTS is PG9.6+. Existing rows backfill to 'generic' so the
-- per-variant breakdown isn't NULL-poisoned.
ALTER TABLE mcp_high_intent_sessions
    ADD COLUMN IF NOT EXISTS claim_variant TEXT;
UPDATE mcp_high_intent_sessions
    SET claim_variant = 'generic'
    WHERE claim_variant IS NULL;
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
    except Exception as e:
        logger.warning("[high_intent_claim] schema ensure failed: %s", e)
        try: c.rollback()
        except Exception: pass


# ── POST /api/v1/mcp/track-paid-hit ───────────────────────────────────

@mcp_high_intent_claim_bp.route("/api/v1/mcp/track-paid-hit", methods=["POST"])
def track_paid_hit():
    """Called by the mcp-server fire-and-forget before returning a paywall
    response. Bumps the per-(session_id, tool) 24h counter. Returns the
    NEW count and whether the high-intent threshold is now crossed.

    Body: {session_id, tool, user_agent?, mcp_client?}

    NOTE: this endpoint is internal-keyed (X-Internal-Key) so a public
    caller can't pump someone else's session count.
    """
    if not _internal_ok(request):
        return jsonify(ok=False, error="forbidden"), 403
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    sid = str(body.get("session_id") or "").strip()[:100]
    tool = str(body.get("tool") or "").strip()[:64]
    if not sid or not tool:
        return jsonify(ok=False, error="missing_session_or_tool"), 400
    ua = str(body.get("user_agent") or "")[:300] or None
    mcp_client = str(body.get("mcp_client") or "")[:80] or None
    # Round 2 (2026-06-07): platform-derived variant for the A/B test. The
    # mcp-server sends this on every call so we can store it the FIRST time
    # we see the (sid, tool) pair (per-row stickiness — a user's first
    # platform wins the variant attribution).
    variant_in = (body.get("variant")
                  or request.args.get("variant") or "")
    variant = _norm_variant(variant_in) if variant_in else None

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # Upsert: if the row exists AND last_hit_at within 24h, increment.
            # If older than 24h, RESET counter to 1 (sliding 24h window).
            # variant is COALESCEd so the very FIRST observation of a session
            # locks the attribution; later same-session calls don't overwrite
            # (a user can't migrate from cursor to claude mid-session anyway).
            cur.execute(
                """
                INSERT INTO mcp_high_intent_sessions
                    (mcp_session_id, tool_name, paid_call_count_24h,
                     first_hit_at, last_hit_at, user_agent, mcp_client,
                     claim_variant)
                VALUES (%s, %s, 1, NOW(), NOW(), %s, %s, %s)
                ON CONFLICT (mcp_session_id, tool_name)
                DO UPDATE SET
                    paid_call_count_24h = CASE
                        WHEN mcp_high_intent_sessions.last_hit_at < NOW() - INTERVAL '24 hours'
                            THEN 1
                        ELSE mcp_high_intent_sessions.paid_call_count_24h + 1
                    END,
                    last_hit_at = NOW(),
                    user_agent = COALESCE(EXCLUDED.user_agent,
                                          mcp_high_intent_sessions.user_agent),
                    mcp_client = COALESCE(EXCLUDED.mcp_client,
                                          mcp_high_intent_sessions.mcp_client),
                    claim_variant = COALESCE(mcp_high_intent_sessions.claim_variant,
                                             EXCLUDED.claim_variant)
                RETURNING paid_call_count_24h, claim_minted_at, claim_used_at
                """,
                (sid, tool, ua, mcp_client, variant),
            )
            r = cur.fetchone() or (0, None, None)
            count = int(r[0] or 0)
            already_minted = r[1] is not None
            already_used = r[2] is not None
        is_high_intent = count >= HIGH_INTENT_THRESHOLD
        return jsonify(
            ok=True,
            count=count,
            is_high_intent=is_high_intent,
            threshold=HIGH_INTENT_THRESHOLD,
            already_minted=already_minted,
            already_used=already_used,
        )
    except Exception as e:
        logger.warning("[track_paid_hit] failed: %s", e)
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


# ── GET /api/v1/mcp/should-mint-claim ─────────────────────────────────

@mcp_high_intent_claim_bp.route("/api/v1/mcp/should-mint-claim", methods=["GET"])
def should_mint_claim():
    """If the (session_id, tool) row has crossed the high-intent threshold
    AND a claim hasn't already been minted in the last 24h, sign + persist
    a claim_token and return it. Idempotent: a second call with the same
    (session_id, tool) returns the EXISTING token (so a retrying agent
    doesn't get N different links).

    Query: ?session_id=...&tool=...  (or POST body with same fields)

    Internal-keyed."""
    if not _internal_ok(request):
        return jsonify(ok=False, error="forbidden"), 403
    sid = str(request.args.get("session_id") or "").strip()[:100]
    tool = str(request.args.get("tool") or "").strip()[:64]
    # Round 2: variant comes from the mcp-server (platform detection happens
    # there). If the row already has a variant, we keep it (sticky). If not,
    # we lock it on mint so per-variant attribution reflects the platform
    # active when the claim URL was issued.
    variant_in = (request.args.get("variant") or "").strip().lower()
    variant = _norm_variant(variant_in) if variant_in else None
    if not sid or not tool:
        return jsonify(ok=False, error="missing_session_or_tool"), 400

    c = _conn()
    if c is None:
        return jsonify(should_mint=False, error="no_db"), 200
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute(
                """SELECT paid_call_count_24h, claim_token, claim_minted_at,
                          claim_used_at, last_hit_at, claim_variant
                     FROM mcp_high_intent_sessions
                    WHERE mcp_session_id = %s AND tool_name = %s""",
                (sid, tool),
            )
            row = cur.fetchone()
            if not row:
                return jsonify(should_mint=False, count=0, threshold=HIGH_INTENT_THRESHOLD)
            count = int(row[0] or 0)
            existing_token = row[1]
            minted_at = row[2]
            used_at = row[3]
            existing_variant = row[5] if len(row) > 5 else None
            # Already used? Don't reissue.
            if used_at is not None:
                return jsonify(
                    should_mint=False, count=count, reason="already_used",
                    threshold=HIGH_INTENT_THRESHOLD,
                )
            # Already minted + still fresh (24h)? Return the same token —
            # idempotent.
            if existing_token and minted_at is not None:
                age_s = (datetime.now(timezone.utc) - minted_at).total_seconds()
                if age_s < CLAIM_TOKEN_TTL_S:
                    return jsonify(
                        should_mint=True,
                        count=count,
                        claim_token=existing_token,
                        claim_url=f"https://dchub.cloud/claim/{existing_token}",
                        reused=True,
                        variant=existing_variant or "generic",
                        threshold=HIGH_INTENT_THRESHOLD,
                    )
            # Below threshold? Don't mint.
            if count < HIGH_INTENT_THRESHOLD:
                return jsonify(
                    should_mint=False, count=count,
                    threshold=HIGH_INTENT_THRESHOLD,
                )
            # Session-level dedup (2026-06-09): a human needs ONE claim link
            # per SESSION, not one per tool. If this session already minted a
            # fresh, unredeemed claim on ANY tool, reuse it instead of signing
            # a new token. Without this, a session that crosses threshold on N
            # tools mints N claims — the inflated mint-rate (e.g. 1700%) symptom.
            cur.execute(
                """SELECT claim_token, claim_minted_at, claim_variant
                     FROM mcp_high_intent_sessions
                    WHERE mcp_session_id = %s
                      AND claim_token IS NOT NULL
                      AND claim_used_at IS NULL
                      AND claim_minted_at IS NOT NULL
                    ORDER BY claim_minted_at DESC
                    LIMIT 1""",
                (sid,),
            )
            sess = cur.fetchone()
            if sess and sess[0] and sess[1] is not None:
                if (datetime.now(timezone.utc) - sess[1]).total_seconds() < CLAIM_TOKEN_TTL_S:
                    return jsonify(
                        should_mint=True,
                        count=count,
                        claim_token=sess[0],
                        claim_url=f"https://dchub.cloud/claim/{sess[0]}",
                        reused=True,
                        variant=(sess[2] if (len(sess) > 2 and sess[2]) else (existing_variant or "generic")),
                        threshold=HIGH_INTENT_THRESHOLD,
                    )
            # Mint a fresh token + persist. Lock the variant on mint if it
            # wasn't already set (fallback: 'generic'). Existing variant
            # wins — first observation is the stickiest signal.
            token = sign_claim_token(sid, tool)
            locked_variant = existing_variant or variant or "generic"
            cur.execute(
                """UPDATE mcp_high_intent_sessions
                      SET claim_token = %s,
                          claim_minted_at = NOW(),
                          claim_variant = COALESCE(claim_variant, %s)
                    WHERE mcp_session_id = %s AND tool_name = %s""",
                (token, locked_variant, sid, tool),
            )
            return jsonify(
                should_mint=True,
                count=count,
                claim_token=token,
                claim_url=f"https://dchub.cloud/claim/{token}",
                reused=False,
                variant=locked_variant,
                threshold=HIGH_INTENT_THRESHOLD,
            )
    except Exception as e:
        logger.warning("[should_mint_claim] failed: %s", e)
        return jsonify(should_mint=False, error=str(e)[:200]), 200
    finally:
        try: c.close()
        except Exception: pass


# ── GET /claim/<token>  + POST /claim/<token> ────────────────────────

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


_CLAIM_FORM_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Unlock __TOOL__ — your DC Hub trial key</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<style>
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
       max-width:560px;margin:0 auto;padding:3rem 1.5rem 6rem;color:#e6e9f5;
       background:rgb(5,8,16);line-height:1.6}
  h1{font-size:1.75rem;margin:0 0 .5rem;font-weight:700;letter-spacing:-.02em;
     color:#fff;line-height:1.2}
  .sub{color:#9aa3bd;margin:0 0 2rem;font-size:.98rem}
  .badge{display:inline-flex;align-items:center;gap:8px;background:rgba(34,211,238,.10);
         color:#22d3ee;padding:6px 14px;border-radius:999px;font-size:.72rem;
         font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin-bottom:1.25rem}
  .badge .dot{width:6px;height:6px;background:#22d3ee;border-radius:50%;
              animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .ctx{background:rgba(34,211,238,.06);border:1px solid rgba(34,211,238,.18);
       border-radius:10px;padding:16px 18px;margin:0 0 1.5rem;font-size:.9rem;
       color:#cbd5ff}
  .ctx strong{color:#22d3ee;font-weight:600}
  input[type=email]{width:100%;padding:14px 16px;border:1.5px solid rgba(255,255,255,.18);
        border-radius:10px;font-size:1rem;background:rgba(255,255,255,.04);color:#fff;
        margin:0 0 12px;font-family:inherit;transition:border-color .15s}
  input[type=email]::placeholder{color:#6a7390}
  input[type=email]:focus{outline:0;border-color:#22d3ee;background:rgba(34,211,238,.05)}
  button{background:linear-gradient(135deg,#22d3ee,#a855f7);color:#0a0f1f;border:0;
         padding:14px 24px;border-radius:10px;font-size:1rem;font-weight:700;
         cursor:pointer;width:100%;font-family:inherit;transition:transform .15s,box-shadow .15s}
  button:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(34,211,238,.25)}
  .err{background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.3);
       border-radius:8px;padding:10px 14px;margin:0 0 14px;color:#fca5a5;font-size:.88rem}
  .note{font-size:.78rem;color:#6a7390;margin-top:2rem;padding-top:1.25rem;
        border-top:1px solid rgba(255,255,255,.06);line-height:1.5}
  code{font-family:"JetBrains Mono","SF Mono",Monaco,Consolas,monospace;font-size:.78rem;
       background:rgba(255,255,255,.06);padding:1px 6px;border-radius:4px;color:#cbd5ff}
</style>
</head>
<body>
  <span class="badge"><span class="dot"></span>High-intent claim · 1 click</span>
  <h1>You've been using <code>__TOOL__</code></h1>
  <p class="sub">Your agent hit this tool __COUNT__ times in the last 24h — clearly the data is useful.
  One email and your trial key is in your inbox in ~60 seconds.</p>

  <div class="ctx"><strong>What you'll get:</strong> a working <code>dch_trial_</code> key (50 calls/day for 7 days),
  plus a copy-paste MCP config for Claude Desktop / Cursor / Cline / Continue.</div>

  __ERR__

  <form method="post" action="">
    <input type="email" name="email" placeholder="you@company.com" required autofocus>
    <button type="submit">Get my trial key →</button>
  </form>

  <p class="note">Trial includes the get_grid_intelligence + get_fiber_intel tools that triggered this offer,
  plus the full free-tier toolset. To go permanent: $9/mo Starter (200 calls/day) or $49/mo Developer
  (500 calls/day) at <a href="https://dchub.cloud/pricing" style="color:#22d3ee">dchub.cloud/pricing</a>.</p>
</body>
</html>
"""


_CLAIM_SUCCESS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Your DC Hub trial key is on its way</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<style>
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
       max-width:560px;margin:0 auto;padding:3rem 1.5rem 6rem;color:#e6e9f5;
       background:rgb(5,8,16);line-height:1.6}
  h1{font-size:1.75rem;margin:0 0 .5rem;font-weight:700;letter-spacing:-.02em;color:#fff;line-height:1.2}
  .sub{color:#9aa3bd;margin:0 0 2rem;font-size:.98rem}
  .key-box{background:rgba(34,211,238,.06);border:1px solid rgba(34,211,238,.3);
           border-radius:10px;padding:18px 20px;margin:0 0 1.5rem;text-align:center}
  .key-box .key{font-family:"JetBrains Mono","SF Mono",Monaco,Consolas,monospace;
                font-size:1rem;color:#22d3ee;font-weight:600;word-break:break-all}
  .key-box .lbl{font-size:.72rem;color:#6a7390;text-transform:uppercase;
                letter-spacing:.12em;margin-bottom:6px}
  pre{background:#0a0f1f;border:1px solid rgba(255,255,255,.08);border-radius:8px;
      padding:14px;font-family:"JetBrains Mono","SF Mono",Monaco,monospace;
      font-size:.78rem;overflow-x:auto;color:#cbd5ff}
  .badge{display:inline-flex;align-items:center;gap:8px;background:rgba(34,197,94,.10);
         color:#22c55e;padding:6px 14px;border-radius:999px;font-size:.72rem;
         font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin-bottom:1.25rem}
  .note{font-size:.85rem;color:#9aa3bd;margin-top:1.5rem;line-height:1.5}
  a{color:#22d3ee}
</style>
</head>
<body>
  <span class="badge">SUCCESS · Key delivered</span>
  <h1>You're in.</h1>
  <p class="sub">We just emailed your trial key to <strong>__EMAIL__</strong>. Check your inbox in ~60 seconds.</p>

  <div class="key-box">
    <div class="lbl">Your trial key</div>
    <div class="key">__KEY__</div>
  </div>

  <p class="sub">Add this to your MCP client config and reconnect. The next call to
  <code>__TOOL__</code> returns the FULL result.</p>

  <pre>{"mcpServers":{"dchub":{"command":"npx","args":["-y","mcp-remote","https://dchub.cloud/mcp"],"env":{"DCHUB_API_KEY":"__KEY__"}}}}</pre>

  <p class="note">Need more? <strong>$9/mo Starter</strong> (200 calls/day) or <strong>$49/mo Developer</strong>
  (500 calls/day) at <a href="https://dchub.cloud/pricing">dchub.cloud/pricing</a>.
  50% off the first 3 months with code <code>DCMCP50_LAUNCH</code>.</p>
</body>
</html>
"""


def _render_form(tool: str, count: int, err: str = "") -> str:
    err_html = (f'<div class="err">{_esc(err)}</div>') if err else ""
    return (_CLAIM_FORM_HTML
            .replace("__TOOL__", _esc(tool or "this tool"))
            .replace("__COUNT__", str(count) if count else "several")
            .replace("__ERR__", err_html))


def _render_success(email: str, key: str, tool: str) -> str:
    return (_CLAIM_SUCCESS_HTML
            .replace("__EMAIL__", _esc(email))
            .replace("__KEY__", _esc(key))
            .replace("__TOOL__", _esc(tool or "your tool")))


def _lookup_claim_row(c, sid: str, tool: str):
    with c.cursor() as cur:
        cur.execute(
            """SELECT paid_call_count_24h, claim_minted_at, claim_used_at,
                      claim_token
                 FROM mcp_high_intent_sessions
                WHERE mcp_session_id = %s AND tool_name = %s""",
            (sid, tool),
        )
        return cur.fetchone()


# ── /claim/<token> kind dispatch — state-of-2026 visitor renderers ──
# Round 2 (2026-06-07): the same /claim/<token> URL now handles MCP session
# tokens AND state-of-2026 web visitor tokens. The shared HMAC verify returns
# `kind`; we render different copy + email body for each. State visitor mint
# delegates to routes.state_visitor_claim (its _mint helper + email body).

def _render_state_form(brief_clicks: int, time_s: int, err: str = "") -> str:
    """State-of-2026 visitor variant of the claim form. Copy references the
    REPORT they were reading (not an MCP tool they were calling)."""
    err_html = (f'<div class="err">{_esc(err)}</div>') if err else ""
    sub = (f"You spent {time_s}+ seconds with the State of 2026 report "
           f"and clicked into {brief_clicks} brief{'s' if brief_clicks != 1 else ''} — "
           "we figure you want the data your way. One email and your trial "
           "key is in your inbox in ~60 seconds.")
    if not brief_clicks and not time_s:
        sub = ("You came in via a one-click claim link from the State of 2026 "
               "report. One email and your trial key is in your inbox.")
    return (_CLAIM_FORM_HTML
            .replace("__TOOL__", "the State of 2026 report")
            .replace("__COUNT__", "")
            .replace("__ERR__", err_html)
            .replace("Your agent hit this tool  times in the last 24h — clearly the data is useful.\n  One email and your trial key is in your inbox in ~60 seconds.", sub))


def _render_state_success(email: str, key: str) -> str:
    return (_CLAIM_SUCCESS_HTML
            .replace("__EMAIL__", _esc(email))
            .replace("__KEY__", _esc(key))
            .replace("__TOOL__", "the State of 2026 data"))


@mcp_high_intent_claim_bp.route("/claim/<token>", methods=["GET"])
def claim_form(token: str):
    """Renders a clean 1-field email form. Validates HMAC + freshness +
    not-already-used before rendering. Bad tokens get a graceful error
    page, not a stack trace."""
    payload = verify_claim_token(token)
    if not payload:
        # Don't 404 — the agent relayed this link, the human deserves a
        # friendly "this expired, here's how to get a key" message.
        body = _render_form("", 0,
                            err="That link expired or was tampered with. "
                                "Get a free dev key at dchub.cloud/signup instead.")
        return Response(body, status=400, mimetype="text/html")
    sid = payload["session_id"]
    tool = payload["tool"]
    kind = payload.get("kind") or KIND_MCP_SESSION

    # ── kind = state_of_2026_visitor → state-flavored form ──
    if kind == KIND_STATE_OF_2026_VISITOR:
        try:
            from routes.state_visitor_claim import fetch_visitor_row
            row = fetch_visitor_row(sid)
        except Exception as e:
            logger.warning("[claim_form] state visitor lookup failed: %s", e)
            row = None
        if row and row.get("claim_used_at") is not None:
            body = _render_state_form(
                row.get("brief_clicks", 0), row.get("time_on_page_seconds", 0),
                err="This claim was already used. Get another key at dchub.cloud/signup.")
            return Response(body, status=410, mimetype="text/html")
        body = _render_state_form(
            (row or {}).get("brief_clicks", 0),
            (row or {}).get("time_on_page_seconds", 0))
        return Response(body, status=200, mimetype="text/html")

    # ── kind = mcp_session → original MCP form ──
    c = _conn()
    if c is None:
        body = _render_form(tool, 3, err="Database temporarily unavailable. Try again in a minute.")
        return Response(body, status=503, mimetype="text/html")
    try:
        _ensure_schema(c)
        row = _lookup_claim_row(c, sid, tool)
        if not row:
            # Token verifies but no row — could happen if the row was purged.
            return Response(_render_form(tool, 3),
                            status=200, mimetype="text/html")
        count = int(row[0] or 0)
        used_at = row[2]
        if used_at is not None:
            body = _render_form(tool, count,
                                err="This claim was already used. "
                                    "Get another key at dchub.cloud/signup.")
            return Response(body, status=410, mimetype="text/html")
        return Response(_render_form(tool, count), status=200, mimetype="text/html")
    finally:
        try: c.close()
        except Exception: pass


@mcp_high_intent_claim_bp.route("/claim/<token>", methods=["POST"])
def claim_submit(token: str):
    """Validates email + token, mints a dch_trial_ key via auto_trial.mint_trial_for_request,
    fires the email via redeem_routes._p99_send_email, marks claim_used_at.
    Idempotent on the (token, claim_used_at) — second POST returns the success page
    with the existing key (not a new one)."""
    # Rate-limit per IP.
    ip = _client_ip(request)
    if not _rate_limit_ok(ip):
        body = _render_form("", 0, err="Too many attempts. Try again in an hour.")
        return Response(body, status=429, mimetype="text/html")

    payload = verify_claim_token(token)
    if not payload:
        body = _render_form("", 0, err="That link expired or was tampered with.")
        return Response(body, status=400, mimetype="text/html")
    sid = payload["session_id"]
    tool = payload["tool"]
    kind = payload.get("kind") or KIND_MCP_SESSION

    email = (request.form.get("email") or "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        if kind == KIND_STATE_OF_2026_VISITOR:
            body = _render_state_form(0, 0, err="Please enter a valid email address.")
        else:
            body = _render_form(tool, 3, err="Please enter a valid email address.")
        return Response(body, status=400, mimetype="text/html")

    # ── kind = state_of_2026_visitor → delegate to state_visitor_claim ──
    # We POST to /api/v1/state-of-2026/claim-email internally (sharing the
    # mint + email path) so the form-style + JSON-style entrypoints can't
    # drift apart. The state module already de-dupes on visitor_session_id.
    if kind == KIND_STATE_OF_2026_VISITOR:
        try:
            from routes.state_visitor_claim import (
                _mint_trial_key_for_email,
                _send_state_of_2026_welcome_email,
            )
        except Exception as e:
            logger.warning("[claim_submit] state_visitor_claim import failed: %s", e)
            body = _render_state_form(0, 0,
                err="Internal error — try again in a minute.")
            return Response(body, status=500, mimetype="text/html")
        c2 = _conn()
        if c2 is None:
            body = _render_state_form(0, 0,
                err="Database temporarily unavailable. Try again in a minute.")
            return Response(body, status=503, mimetype="text/html")
        try:
            api_key, mint_err = _mint_trial_key_for_email(email, c2, request)
            ok_send, send_detail = _send_state_of_2026_welcome_email(email, api_key)
            # Persist on state_visitor_intent.
            try:
                with c2.cursor() as cur:
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
                        (token, email, api_key, sid),
                    )
            except Exception as e:
                logger.warning("[claim_submit] state persist failed: %s", e)
            logger.info("[claim_submit] state_of_2026 minted email=%s key=%s send=%s mint_err=%s",
                        email, (api_key or "")[:20] + "...", ok_send, mint_err)
            return Response(_render_state_success(email, api_key),
                            status=200, mimetype="text/html")
        finally:
            try: c2.close()
            except Exception: pass

    # ── kind = mcp_session → original MCP path ──
    c = _conn()
    if c is None:
        body = _render_form(tool, 3, err="Database temporarily unavailable. Try again in a minute.")
        return Response(body, status=503, mimetype="text/html")
    try:
        _ensure_schema(c)
        row = _lookup_claim_row(c, sid, tool)
        if not row:
            body = _render_form(tool, 3,
                                err="Claim record not found — token may have expired.")
            return Response(body, status=410, mimetype="text/html")
        used_at = row[2]
        if used_at is not None:
            # Already used — fetch the prior key + render success page.
            with c.cursor() as cur:
                cur.execute(
                    """SELECT minted_api_key, claim_email
                         FROM mcp_high_intent_sessions
                        WHERE mcp_session_id = %s AND tool_name = %s""",
                    (sid, tool),
                )
                r = cur.fetchone() or (None, None)
            existing_key = r[0] or ""
            existing_email = r[1] or email
            if existing_key:
                return Response(_render_success(existing_email, existing_key, tool),
                                status=200, mimetype="text/html")
            body = _render_form(tool, 3,
                                err="This claim was already used.")
            return Response(body, status=410, mimetype="text/html")

        # ── Mint a trial key ──
        # Use the existing auto_trial.mint_trial_for_request helper so we
        # share the (ip_hash, ua) dedup window + funnel attribution table.
        # client_name = mcp_client we recorded at track-paid-hit time;
        # operator_email = the human's email (the conversion bridge).
        api_key = None
        mint_error = None
        try:
            from routes.auto_trial import mint_trial_for_request as _mint
            ua_for_mint = (request.headers.get("User-Agent") or "")[:200]
            mint_result = _mint(
                req=request,
                tool_name=tool,
                client_name="high-intent-claim",
                operator_email=email,
            )
            if isinstance(mint_result, dict) and mint_result.get("ok"):
                api_key = mint_result.get("api_key")
            else:
                mint_error = (mint_result or {}).get("error") if isinstance(mint_result, dict) else None
        except Exception as e:
            mint_error = f"mint_exception: {type(e).__name__}: {e}"
            logger.warning("[claim_submit] mint failed: %s", e)

        if not api_key:
            # Fallback: synthesize a trial key directly so the form never
            # leaves the human empty-handed. The auto_trial_keys row creation
            # might have failed (DB blip) but we still mint a unique key and
            # try to write the row at the end — if that fails too, the user
            # can still use the key (validation will fall back to the
            # mcp_dev_keys path).
            api_key = "dch_trial_" + secrets.token_urlsafe(24).replace("_", "x").replace("-", "x")[:32]
            try:
                with c.cursor() as cur:
                    cur.execute(
                        """INSERT INTO auto_trial_keys
                             (api_key, minted_for_tool, request_ip_hash, request_ua,
                              expires_at, operator_email, client_name)
                           VALUES (%s, %s, %s, %s, NOW() + INTERVAL '7 days', %s, %s)
                           ON CONFLICT (api_key) DO NOTHING""",
                        (api_key, tool,
                         hashlib.sha256(ip.encode()).hexdigest()[:16],
                         (request.headers.get("User-Agent") or "")[:200],
                         email, "high-intent-claim-fallback"),
                    )
            except Exception as e:
                logger.warning("[claim_submit] fallback insert failed: %s", e)

        # ── Send the email ──
        email_status = "skipped"
        try:
            from routes.redeem_routes import _p99_send_email as _send
            ok_send, detail = _send(email, api_key, [tool])
            email_status = "sent" if ok_send else f"failed: {detail[:200]}"
        except Exception as e:
            email_status = f"exception: {type(e).__name__}: {e}"
            logger.warning("[claim_submit] email send failed: %s", e)

        # ── Mark claim_used_at + persist email/key ──
        try:
            with c.cursor() as cur:
                cur.execute(
                    """UPDATE mcp_high_intent_sessions
                          SET claim_used_at = NOW(),
                              claim_email = %s,
                              minted_api_key = %s
                        WHERE mcp_session_id = %s AND tool_name = %s""",
                    (email, api_key, sid, tool),
                )
        except Exception as e:
            logger.warning("[claim_submit] mark-used failed: %s", e)

        # r74 (2026-06-07): emit CRM reverse-ETL capture event.
        # Fail-soft — a CRM hiccup cannot break the conversion flow.
        try:
            from routes.crm_reverse_etl import capture_event as _crm_capture
            _crm_capture("mcp_high_intent", {
                "email": email, "session_id": sid,
                "tool": tool, "claim_token": token[:16],
            })
        except Exception as _e_crm:
            logger.debug("[claim_submit] crm capture skipped: %s", _e_crm)

        logger.info("[claim_submit] minted token=%s tool=%s email=%s key=%s email_status=%s mint_error=%s",
                    token[:16], tool, email, api_key[:20] + "..." if api_key else None,
                    email_status, mint_error)

        return Response(_render_success(email, api_key, tool),
                        status=200, mimetype="text/html")
    finally:
        try: c.close()
        except Exception: pass


# ── GET /api/v1/mcp/high-intent/stats ────────────────────────────────

@mcp_high_intent_claim_bp.route("/api/v1/mcp/high-intent/stats", methods=["GET"])
def high_intent_stats():
    """Public funnel KPIs:
       * high_intent_sessions_30d
       * claims_minted_30d
       * claim_minted_rate_30d  (claims / high_intent_sessions)
       * claims_used_30d
       * claim_to_paid_rate_30d  (paid conversions where the email matches
         a claim_used_at row within 14d after the claim was used)
    """
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    out = {
        "high_intent_sessions_30d": 0,
        "claims_minted_30d": 0,
        "claims_used_30d": 0,
        "claim_minted_rate_30d_pct": 0.0,
        "claim_to_paid_rate_30d_pct": 0.0,
        "claim_to_paid_30d": 0,
    }
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # All rows where the count crossed threshold in last 30d.
            try:
                cur.execute(
                    """SELECT COUNT(*) FROM mcp_high_intent_sessions
                        WHERE last_hit_at >= NOW() - INTERVAL '30 days'
                          AND paid_call_count_24h >= %s""",
                    (HIGH_INTENT_THRESHOLD,),
                )
                out["high_intent_sessions_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            try:
                cur.execute(
                    """SELECT COUNT(*) FROM mcp_high_intent_sessions
                        WHERE claim_minted_at IS NOT NULL
                          AND claim_minted_at >= NOW() - INTERVAL '30 days'""",
                )
                out["claims_minted_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            try:
                cur.execute(
                    """SELECT COUNT(*) FROM mcp_high_intent_sessions
                        WHERE claim_used_at IS NOT NULL
                          AND claim_used_at >= NOW() - INTERVAL '30 days'""",
                )
                out["claims_used_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            # claim → paid conversion: the human entered email X on a claim
            # in last 30d, and there's an mcp_conversions row OR users row
            # with same email + plan != 'free' (created at-or-after claim_used_at).
            try:
                cur.execute(
                    """SELECT COUNT(DISTINCT h.claim_email)
                         FROM mcp_high_intent_sessions h
                         JOIN users u ON LOWER(u.email) = LOWER(h.claim_email)
                        WHERE h.claim_used_at IS NOT NULL
                          AND h.claim_used_at >= NOW() - INTERVAL '30 days'
                          AND COALESCE(u.plan, 'free') NOT IN ('free','')
                          AND COALESCE(u.created_at, NOW()) >= h.claim_used_at - INTERVAL '7 days'
                    """,
                )
                out["claim_to_paid_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
        # Derived rates.
        if out["high_intent_sessions_30d"] > 0:
            out["claim_minted_rate_30d_pct"] = round(
                100.0 * out["claims_minted_30d"] / out["high_intent_sessions_30d"], 2)
        if out["claims_used_30d"] > 0:
            out["claim_to_paid_rate_30d_pct"] = round(
                100.0 * out["claim_to_paid_30d"] / out["claims_used_30d"], 2)
        return jsonify(ok=True, **out, threshold=HIGH_INTENT_THRESHOLD)
    finally:
        try: c.close()
        except Exception: pass


# ── GET /api/v1/admin/mcp/claim-variant-conversion ────────────────────
# Round 2 (2026-06-07): per-platform A/B reporting. Surfaces:
#   minted (claim URLs sent) → used (human entered email + key minted)
#                            → paid (joined to users.email + plan != free)
# Window: ?days=N (default 30, max 365). Admin-keyed.

@mcp_high_intent_claim_bp.route(
    "/api/v1/admin/mcp/claim-variant-conversion", methods=["GET"])
def claim_variant_conversion():
    if not _internal_ok(request):
        return jsonify(ok=False, error="forbidden"), 403
    try:
        days = max(1, min(365, int(request.args.get("days") or "30")))
    except Exception:
        days = 30

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_schema(c)
        # Build one row per variant. We left-join to users.email to attribute
        # paid conversions back to the variant the human first saw. The COALESCE
        # on claim_variant ('generic' default) covers any backfill gaps.
        rows = []
        with c.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(h.claim_variant,''), 'generic') AS variant,
                        COUNT(*) FILTER (WHERE h.claim_minted_at IS NOT NULL
                                         AND h.claim_minted_at >= NOW() - INTERVAL '%s days') AS minted,
                        COUNT(*) FILTER (WHERE h.claim_used_at IS NOT NULL
                                         AND h.claim_used_at   >= NOW() - INTERVAL '%s days') AS used,
                        COUNT(DISTINCT CASE
                            WHEN h.claim_used_at IS NOT NULL
                                 AND h.claim_used_at >= NOW() - INTERVAL '%s days'
                                 AND u.email IS NOT NULL
                                 AND COALESCE(u.plan,'free') NOT IN ('free','')
                            THEN LOWER(h.claim_email)
                        END) AS paid
                    FROM mcp_high_intent_sessions h
                    LEFT JOIN users u ON LOWER(u.email) = LOWER(h.claim_email)
                    GROUP BY 1
                    ORDER BY minted DESC, used DESC, paid DESC
                    """ % (days, days, days)
                )
                for r in cur.fetchall():
                    variant, minted, used, paid = (r[0] or "generic",
                                                   int(r[1] or 0),
                                                   int(r[2] or 0),
                                                   int(r[3] or 0))
                    use_rate = round(100.0 * used / minted, 2) if minted else 0.0
                    paid_rate = round(100.0 * paid / minted, 2) if minted else 0.0
                    # Only include variants that have actually been minted —
                    # an empty 'generic' row from pre-Round-2 data is fine,
                    # but variants that have NEVER fired are skipped to keep
                    # the dashboard signal-to-noise high.
                    if minted == 0 and used == 0 and paid == 0:
                        continue
                    rows.append({
                        "variant": variant,
                        "minted": minted,
                        "used": used,
                        "paid": paid,
                        "use_rate_pct": use_rate,
                        "paid_rate_pct": paid_rate,
                    })
            except Exception as e:
                logger.warning("[claim_variant_conversion] query failed: %s", e)
                try: c.rollback()
                except Exception: pass

        # Ensure every known variant appears (zero-rows) so the dashboard can
        # show "no data yet" for variants that haven't fired — easier to
        # diagnose UA-detection gaps.
        seen = {r["variant"] for r in rows}
        for v in sorted(VALID_VARIANTS):
            if v not in seen:
                rows.append({"variant": v, "minted": 0, "used": 0, "paid": 0,
                             "use_rate_pct": 0.0, "paid_rate_pct": 0.0})

        # Totals across all variants (handy for the dashboard header).
        total_minted = sum(r["minted"] for r in rows)
        total_used   = sum(r["used"]   for r in rows)
        total_paid   = sum(r["paid"]   for r in rows)
        return jsonify(
            ok=True,
            window_days=days,
            variants=rows,
            totals={
                "minted": total_minted,
                "used": total_used,
                "paid": total_paid,
                "use_rate_pct": round(100.0 * total_used / total_minted, 2) if total_minted else 0.0,
                "paid_rate_pct": round(100.0 * total_paid / total_minted, 2) if total_minted else 0.0,
            },
            threshold=HIGH_INTENT_THRESHOLD,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        try: c.close()
        except Exception: pass


# ── GET /api/v1/admin/mcp/high-intent/step-drop ───────────────────────
# 2026-06-07: step-by-step drop-off monitor so the brain can scream the
# instant the claim funnel breaks again. Reports a 7-step waterfall:
#
#   1. claims_minted   — claim URLs sent (token generated, X-DC-Claim-URL
#                        emitted to the AI agent in the paywall response)
#   2. page_viewed     — human actually opened /claim/<token>
#                        (proxy: claim_used_at IS NOT NULL, OR a tracked
#                        page_view event on this row — we use the former
#                        as a strict superset; a click without submit isn't
#                        currently tracked but the gap matters most when
#                        the page itself is broken which IS visible from
#                        the should_mint_rate / used delta)
#   3. email_submitted — POST /claim/<token> with a valid email
#                        (proxy: claim_email IS NOT NULL on this row)
#   4. key_issued      — auto_trial.mint_trial_for_request returned a key
#                        (proxy: minted_api_key IS NOT NULL on this row)
#   5. first_api_call  — that key actually made an API call after issuance
#                        (joined to mcp_call_log.api_key or auto_trial_keys.last_used_at)
#   6. upgrade_click   — the human hit a Stripe checkout link from a
#                        paywall surfaced AFTER the trial issuance
#                        (proxy: mcp_upgrade_signals.stripe_clicked_at
#                        for the same email within 14 days)
#   7. paid            — joined to users.plan != 'free' (same email window)
#
# The drop_pct field on each row tells the brain WHERE the funnel is
# bleeding. If step 1→2 is the killer (the 19/0/0 we just fixed) this
# would have been a 100% drop alarm on the very first day.
#
# All windows: ?days=N (default 30, max 365). Admin-keyed.

@mcp_high_intent_claim_bp.route(
    "/api/v1/admin/mcp/high-intent/step-drop", methods=["GET"])
def high_intent_step_drop():
    """Step-by-step drop-off monitor. The single page the brain checks
    every 15 min to scream the instant the claim funnel breaks.

    Returns:
      {ok, window_days, steps: [{step, label, count, drop_pct, drop_from_prev},
                                ...],
       overall_conversion_pct,
       killer_step: <name of the step with the biggest drop_from_prev>,
       alarm: bool}
    """
    if not _internal_ok(request):
        return jsonify(ok=False, error="forbidden"), 403
    try:
        days = max(1, min(365, int(request.args.get("days") or "30")))
    except Exception:
        days = 30

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503

    steps_out = []
    try:
        _ensure_schema(c)
        # Pull the 7 counts from a single CTE-style probe per step so a
        # missing column on a brand-new schema can't blank the whole page.
        # Each scalar is wrapped in its own try/except.
        def _scalar(sql: str, args: tuple = ()) -> int:
            try:
                with c.cursor() as cur:
                    cur.execute(sql, args)
                    row = cur.fetchone()
                return int((row or [0])[0] or 0)
            except Exception as e:
                logger.debug("[step_drop] probe failed: %s -- %s", sql[:60], e)
                try: c.rollback()
                except Exception: pass
                return 0

        n_minted = _scalar(
            """SELECT COUNT(*) FROM mcp_high_intent_sessions
                WHERE claim_minted_at IS NOT NULL
                  AND claim_minted_at >= NOW() - INTERVAL '%s days'""" % days)

        # Step 2: we don't yet track raw page-views separately from POSTs.
        # As a conservative proxy: claim_used_at IS NOT NULL (form-submit).
        # The brain can compare n_used vs n_minted directly — that's the
        # 19/0/0 alarm we just lived through.
        n_used = _scalar(
            """SELECT COUNT(*) FROM mcp_high_intent_sessions
                WHERE claim_used_at IS NOT NULL
                  AND claim_used_at >= NOW() - INTERVAL '%s days'""" % days)
        n_page_viewed = n_used  # conservative under-count; see note above.

        n_email = _scalar(
            """SELECT COUNT(*) FROM mcp_high_intent_sessions
                WHERE claim_email IS NOT NULL
                  AND claim_used_at IS NOT NULL
                  AND claim_used_at >= NOW() - INTERVAL '%s days'""" % days)

        n_key = _scalar(
            """SELECT COUNT(*) FROM mcp_high_intent_sessions
                WHERE minted_api_key IS NOT NULL
                  AND claim_used_at IS NOT NULL
                  AND claim_used_at >= NOW() - INTERVAL '%s days'""" % days)

        # Step 5: trial key actually made an API call. Join to auto_trial_keys
        # (where the trial keys live with a last_used_at column).
        n_first_call = _scalar(
            """SELECT COUNT(DISTINCT h.minted_api_key)
                 FROM mcp_high_intent_sessions h
                 JOIN auto_trial_keys a ON a.api_key = h.minted_api_key
                WHERE h.minted_api_key IS NOT NULL
                  AND h.claim_used_at >= NOW() - INTERVAL '%s days'
                  AND a.last_used_at IS NOT NULL""" % days)

        # Step 6: same email later hit Stripe checkout.
        n_upgrade_click = _scalar(
            """SELECT COUNT(DISTINCT LOWER(h.claim_email))
                 FROM mcp_high_intent_sessions h
                 JOIN mcp_upgrade_signals u
                   ON LOWER(u.operator_email) = LOWER(h.claim_email)
                WHERE h.claim_email IS NOT NULL
                  AND h.claim_used_at >= NOW() - INTERVAL '%s days'
                  AND u.stripe_clicked_at IS NOT NULL
                  AND u.stripe_clicked_at >= h.claim_used_at - INTERVAL '1 day'
                  AND u.stripe_clicked_at <= h.claim_used_at + INTERVAL '14 days'""" % days)

        # Step 7: same email is paying.
        n_paid = _scalar(
            """SELECT COUNT(DISTINCT LOWER(h.claim_email))
                 FROM mcp_high_intent_sessions h
                 JOIN users u ON LOWER(u.email) = LOWER(h.claim_email)
                WHERE h.claim_email IS NOT NULL
                  AND h.claim_used_at >= NOW() - INTERVAL '%s days'
                  AND COALESCE(u.plan, 'free') NOT IN ('free','')""" % days)

        raw_steps = [
            ("claims_minted",   "Claim URL minted + sent to agent",   n_minted),
            ("page_viewed",     "Human opened the claim page",        n_page_viewed),
            ("email_submitted", "Human submitted email",              n_email),
            ("key_issued",      "Trial key issued",                   n_key),
            ("first_api_call",  "Trial key made first API call",      n_first_call),
            ("upgrade_click",   "Human clicked Stripe checkout",      n_upgrade_click),
            ("paid",            "Conversion: plan != 'free'",          n_paid),
        ]

        prev = None
        killer_drop = -1.0
        killer = None
        for (name, label, count) in raw_steps:
            if prev is None or prev == 0:
                drop_from_prev = 0.0
            else:
                drop_from_prev = round(100.0 * (prev - count) / prev, 2)
            drop_pct = (round(100.0 * (n_minted - count) / n_minted, 2)
                        if n_minted else 0.0)
            steps_out.append({
                "step":            name,
                "label":           label,
                "count":           count,
                "drop_pct":        drop_pct,
                "drop_from_prev":  drop_from_prev,
            })
            if drop_from_prev > killer_drop and name != "claims_minted":
                killer_drop = drop_from_prev
                killer = name
            prev = count

        overall_conv = (round(100.0 * n_paid / n_minted, 2)
                        if n_minted else 0.0)
        # Alarm: minted > 5 AND used == 0 (the 19/0/0 alarm), OR any
        # single step drops >95% off the prior step.
        alarm = (n_minted > 5 and n_used == 0) or killer_drop > 95.0

        return jsonify(
            ok=True,
            window_days=days,
            steps=steps_out,
            overall_conversion_pct=overall_conv,
            killer_step=killer,
            killer_drop_pct=round(killer_drop, 2) if killer else 0.0,
            alarm=alarm,
            threshold=HIGH_INTENT_THRESHOLD,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info("[high_intent_claim] ready · threshold=%s · variants=%s · "
                "POST /api/v1/mcp/track-paid-hit · "
                "GET /api/v1/mcp/should-mint-claim · GET/POST /claim/<token> · "
                "GET /api/v1/mcp/high-intent/stats · "
                "GET /api/v1/admin/mcp/claim-variant-conversion · "
                "GET /api/v1/admin/mcp/high-intent/step-drop",
                HIGH_INTENT_THRESHOLD, sorted(VALID_VARIANTS))


_smoke()
