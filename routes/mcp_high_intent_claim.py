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

from flask import Blueprint, Response, jsonify, redirect, request
from mcp_calls_deloop import real_ua_predicate
from routes._swallowed_writes import note_swallowed_write


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


# r-claim-internal-guard (2026-06-21): DC Hub's OWN automated traffic crosses the
# 2-paid-hit high-intent threshold but has NO human to ever open the /claim URL.
# It was minting ~81% of all claims (44/54 in 30d, all `dchub-regression-test`)
# and faking a 98.2% funnel "drop". Keep internal/CI/probe/monitor clients OUT of
# the high-intent funnel so it reflects only real, human-bearing prospects.
_INTERNAL_CLAIM_CLIENT_RE = re.compile(
    r"dchub|self.?heal|regression|verify.?test|trial.?test|smoke|e2e"
    r"|sweep|probe|\bqa\b|monitor|health.?check|watchdog|canary"
    r"|uptimerobot|fleet.?view|\bfv\b|headless|brain",
    re.I,
)


def _is_internal_claim_client(mcp_client: str | None, user_agent: str | None) -> bool:
    """True for our own monitors / CI / probe traffic — never mint a claim for them."""
    blob = f"{mcp_client or ''} {user_agent or ''}"
    return bool(_INTERNAL_CLAIM_CLIENT_RE.search(blob))


# r-claim-script-guard (2026-06-23): the internal-client guard above catches our
# NAMED automation (dchub-/regression/brain) but NOT raw HTTP-library traffic —
# python-httpx / Python-urllib / curl scripts that hit paid tools, cross the
# high-intent threshold, mint a claim URL, and then NEVER open it because there
# is no human and no browser. Live audit (2026-06-23): of 123 claims minted in
# 30d, ~93 were raw httpx/urllib scripts (client names "clawith","gating-audit",
# "p",…) and only ~13 were real agents-with-humans (claude/cursor/opencode).
# Counting the scripts faked a -99% "leak". A genuine prospect always connects
# THROUGH an MCP client (Claude/Cursor/Cline/…), never raw httpx — so a raw
# scripting UA is a reliable "no human here" signal. We neither MINT for them nor
# COUNT them. (Bare 'node' is mcp-remote — a REAL transport — deliberately NOT in
# the list.) The SQL twin _SCRIPT_UA_SQL is used by the step-drop metric query so
# the gate and the dashboard agree by construction.
_SCRIPT_UA_TOKENS = (
    "python-httpx", "python-urllib", "urllib", "curl/", "wget", "libwww",
    "node-fetch", "undici", "axios", "got/", "go-http", "okhttp", "java/",
    "requests/", "aiohttp", "scrapy", "postman", "httpie", "restsharp",
)
_SCRIPT_UA_RE  = re.compile("|".join(re.escape(t) for t in _SCRIPT_UA_TOKENS), re.I)
_SCRIPT_UA_SQL = "(" + "|".join(_SCRIPT_UA_TOKENS) + ")"   # POSIX ~* alternation


def _is_non_human_client(mcp_client: str | None, user_agent: str | None) -> bool:
    """True for traffic with no human able to open a /claim browser link —
    our own internal automation OR a raw HTTP-library/scripting UA. Used to
    keep both the MINT decision and the funnel METRIC honest (same predicate)."""
    if _is_internal_claim_client(mcp_client, user_agent):
        return True
    return bool(_SCRIPT_UA_RE.search(user_agent or ""))


# SQL twin of _is_non_human_client for the step-drop METRIC query, so the
# dashboard counts exactly the real prospects the mint gate admits. POSIX ~*
# has no \b, so internal tokens are bare; this errs toward UNDER-counting (a
# borderline client is dropped, never fake-inflated). Includes this-session
# test labels (audit/fix2/gating/adv-check/hi-claim/funnel-diag).
_INTERNAL_CLIENT_SQL = (
    "(dchub|self.?heal|regression|verify|trial.?test|smoke|e2e|sweep|probe|"
    "monitor|health.?check|watchdog|canary|uptimerobot|fleet.?view|headless|"
    "brain|audit|fix2|gating|adv.?check|hi.?claim|funnel-diag|clawith|friction)"
)


def _hi_real_sql(prefix: str = "") -> str:
    """SQL boolean (no leading AND) TRUE for real human-bearing high-intent
    rows. prefix = optional table alias incl trailing dot (e.g. 'h.').

    r-identity-verdict (2026-07-03): on top of the local regex, this now also
    applies the CANONICAL is_real_external building blocks from
    mcp_calls_deloop — the same rendered predicates the mcp_calls_identity
    view is generated from — to this table's mcp_client/user_agent columns.
    The 07-03 deep dive found QA clients (gating-audit / fix2-verify /
    hi-claim-test / clientx-friction-audit / clawith, UA render-verify)
    counted as "real paywall-hit sessions"; most were caught locally, but
    'clawith' only via its UA, and every new QA tag needed a second hand-edit
    here. Reusing the deloop constants means one list, two verdicts, no
    drift. Regex form only (internal_tag_regex_predicate, not the LIKE
    variant) — this fragment is embedded in queries WITH bound params, where
    a literal % would be eaten by psycopg2 paramstyle substitution."""
    p = prefix
    parts = [
        f"COALESCE({p}mcp_client,'') !~* '{_INTERNAL_CLIENT_SQL}'",
        f"COALESCE({p}user_agent,'') !~* '{_INTERNAL_CLIENT_SQL}'",
        f"COALESCE({p}user_agent,'') !~* '{_SCRIPT_UA_SQL}'",
    ]
    try:
        from mcp_calls_deloop import (
            internal_tag_regex_predicate as _tag_pred,
            real_ua_predicate as _ua_pred,
        )
        parts.append(_tag_pred(f"{p}mcp_client"))
        parts.append(_ua_pred(f"{p}user_agent"))
    except Exception:  # pragma: no cover — deloop module missing must never
        pass           # blank the funnel; the local regex still applies.
    return " AND ".join(parts)


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
    HUMAN_VIEW_TTL_S,
    KIND_HUMAN_VIEW,
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


# ── Shell #44 r-two-artifacts (2026-07-30): the HUMAN-audience link ───
# One single-use claim token cannot serve both audiences: server.mjs
# auto-redeems the claim in median 0.85s (its contract: "binds the trial key
# with NO human page-open"), so every human click since launch hit 410 and
# claim_page_opened_at fired 0× all-time. The agent path is BY DESIGN and
# unchanged (see reference: machine-mediated handoff). This mints a SECOND,
# durable artifact for the human: 7-day TTL, multi-open, binds NOTHING on
# open — it makes "human acted" measurable for the first time, which is why
# the funnel stage carries a definition_version bump the day this shipped.
# Human tokens are stateless (HMAC-verified, never stored): re-minting for
# the same (session, tool) yields a different string that resolves to the
# same row, and /relay counts opens on the row, not the token.

def sign_human_view_token(mcp_session_id: str, tool_name: str,
                          ts: int | None = None) -> str:
    return _shared_sign(
        kind=KIND_HUMAN_VIEW,
        session_id=mcp_session_id,
        extra=tool_name,
        ts=ts,
    )


def verify_human_view_token(token: str) -> dict | None:
    """Human-audience tokens ONLY — returns None for agent/legacy kinds so a
    claim token pasted into /relay (or vice versa) can never cross audiences."""
    payload = _shared_verify(token, max_age_s=HUMAN_VIEW_TTL_S)
    if not payload or payload.get("kind") != KIND_HUMAN_VIEW:
        return None
    return {
        "session_id": payload["session_id"],
        "tool":       payload["extra"],
        "ts":         payload["ts"],
    }


def _human_handoff_fields(sid: str, tool: str) -> dict:
    """The mint-payload fields that give the HUMAN link a chance of being
    seen: agents render tool JSON, humans don't read envelopes — so the note
    tells the AGENT to surface the link, in words the agent can relay
    verbatim. claim_page_opened_at = 0 all-time says the old link never even
    reached human eyes; a durable link only fixes half of that."""
    return {
        "human_url": f"https://dchub.cloud/relay/{sign_human_view_token(sid, tool)}",
        "human_note": (
            "SHOW human_url TO YOUR HUMAN (chat text, not just tool output): "
            "it is their multi-use link — safe to open anytime this week, "
            "binds nothing, shows what a key unlocks and where to get one. "
            "claim_url stays yours (single-use, auto-redeemed)."
        ),
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
-- r-claim-page-open (2026-06-24): record the FIRST time a human opens the /claim
-- page (GET), so the funnel can separate "opened the page" from "submitted the
-- email". Runs in prod because _ensure_schema uses raw psycopg2 (no SKIP_DDL gate).
ALTER TABLE mcp_high_intent_sessions
    ADD COLUMN IF NOT EXISTS claim_page_opened_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_mhis_page_opened_at
    ON mcp_high_intent_sessions(claim_page_opened_at DESC)
    WHERE claim_page_opened_at IS NOT NULL;
-- r-claim-notify (2026-06-25): stamp the moment we fire the out-of-band operator
-- notification for a fresh mint, so it fires EXACTLY ONCE per claim (the mint path
-- claims it via a conditional UPDATE ... WHERE claim_notified_at IS NULL).
ALTER TABLE mcp_high_intent_sessions
    ADD COLUMN IF NOT EXISTS claim_notified_at TIMESTAMPTZ;
-- Shell #44 r-two-artifacts (2026-07-30): the HUMAN-audience view link's
-- instrument. first_opened stamps once (the funnel's human_acted v2 reads it);
-- opens counts every render (multi-open is the point — a durable link a human
-- can revisit). The token itself is stateless (HMAC), so no token column.
ALTER TABLE mcp_high_intent_sessions
    ADD COLUMN IF NOT EXISTS human_view_first_opened_at TIMESTAMPTZ;
ALTER TABLE mcp_high_intent_sessions
    ADD COLUMN IF NOT EXISTS human_view_opens INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_mhis_human_opened_at
    ON mcp_high_intent_sessions(human_view_first_opened_at DESC)
    WHERE human_view_first_opened_at IS NOT NULL;
-- r-both-artifacts (2026-08-16): UA of the first REAL open of /relay — the
-- funnel's human_acted v3 instrument for this artifact. first_opened_at alone
-- cannot tell a probe from a person (all 4 all-time stamps were probes:
-- cursor render-verify, Grok probes, an indexer), so relay_view stamps the
-- first UA that passes mcp_calls_deloop.real_ua_predicate; probe opens never
-- occupy the slot, and the funnel re-applies the predicate at read time so a
-- family added later still retro-excludes. Pre-v3 stamps stay NULL here.
ALTER TABLE mcp_high_intent_sessions
    ADD COLUMN IF NOT EXISTS human_view_first_ua TEXT;
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
    # r-claim-internal-guard (2026-06-21) + r-claim-script-guard (2026-06-23):
    # never enter no-human traffic into the high-intent funnel — our OWN automation
    # (self-heal / regression / QA / probe / brain) OR raw HTTP-library scripts
    # (python-httpx / urllib / curl). Both cross the threshold and mint claims with
    # no human to ever open the link, faking a ~99% drop. Skip recording entirely so
    # the funnel = real, browser-bearing prospects only.
    if _is_non_human_client(mcp_client, ua):
        _why = "internal_client" if _is_internal_claim_client(mcp_client, ua) else "scripting_ua"
        return jsonify(ok=True, count=0, is_high_intent=False,
                       threshold=HIGH_INTENT_THRESHOLD, skipped=_why)
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
                VALUES (%s, %s, 1, NOW() ON CONFLICT DO NOTHING, NOW(), %s, %s, %s)
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
                        **_human_handoff_fields(sid, tool),
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
                        **_human_handoff_fields(sid, tool),
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
            # r-claim-notify (2026-06-25): fire ONE out-of-band operator notification
            # per fresh mint. The claim URL otherwise only reaches the AI agent, which
            # rarely surfaces it to a human (the root of the 18-day dead-funnel streak).
            # Claim the notify slot atomically so a racing call can't double-send, then
            # hand off fire-and-forget. No-op unless DCHUB_CLAIM_NOTIFY=1. Never blocks
            # or breaks the mint (autocommit conn; fully wrapped).
            try:
                cur.execute(
                    """UPDATE mcp_high_intent_sessions
                          SET claim_notified_at = NOW()
                        WHERE mcp_session_id = %s AND tool_name = %s
                          AND claim_notified_at IS NULL
                        RETURNING 1""",
                    (sid, tool),
                )
                if cur.fetchone():
                    from routes.claim_notify import notify_operator_of_claim
                    notify_operator_of_claim(sid, tool, token, count, locked_variant)
            except Exception as _ne:
                logger.warning("[should_mint_claim] claim-notify hook failed: %s", _ne)
            return jsonify(
                should_mint=True,
                count=count,
                claim_token=token,
                claim_url=f"https://dchub.cloud/claim/{token}",
                reused=False,
                variant=locked_variant,
                threshold=HIGH_INTENT_THRESHOLD,
                **_human_handoff_fields(sid, tool),
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
  <p class="sub">Your agent hit this tool __COUNT__ times in the last 24h — it's clearly load-bearing.
  Your trial unlocks __VALUE__.</p>

  <div class="ctx">📍 <strong>__BLINDSPOT__</strong> — your agent can read the numbers, but the full
  interactive view only renders here. One email → a working <code>dch_trial_</code> key
  (50 calls/day, 7 days) + a copy-paste MCP config for Claude Desktop / Cursor / Cline, in ~60 seconds.</div>

  __ERR__

  <a href="https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i?client_reference_id=ref_claim__tool___TOOL_REF__"
     style="display:block;background:linear-gradient(135deg,#22d3ee,#a855f7);color:#0a0f1f;text-align:center;padding:15px;border-radius:10px;font-weight:700;text-decoration:none;font-size:1.02rem;margin-bottom:6px">⚡ Get instant full access — $10 · 1,000 calls →</a>
  <div style="text-align:center;color:#94a3b8;font-size:.78rem;margin-bottom:18px">One-time · no subscription · <strong>no email needed</strong></div>

  <div style="text-align:center;color:#64748b;font-size:.8rem;margin:0 0 10px">— or get a free 7-day trial key by email —</div>
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


# r-claim-resultforward (2026-06-26): the /claim page showed only the tool name +
# a 24h count — a context-free email gate. Map each high-intent tool to (a) the
# concrete value the trial unlocks and (b) the VISUAL layer the agent can't render
# in a chat window (the honest, true hook — no fabricated results, no fake scarcity).
_TOOL_VALUE = {
    "get_grid_intelligence":     ("the full per-substation headroom, interconnection-queue depth and time-to-power for your market", "The live grid map + load curves"),
    "get_fiber_intel":           ("every carrier, route and IX within reach of your site, with redundancy scoring", "The dark-fiber route overlay"),
    "get_fiber_readiness":       ("the full connectivity score — carriers, routes, IXs and a lead-in plan for your site", "The fiber route + lead-in map"),
    "plan_fiber_leadin":         ("the full lead-in plan — routes, carriers and build estimate for your site", "The fiber lead-in map"),
    "analyze_site":              ("the complete site score — power, fiber, water, gas, tax and hazard layers for your coordinates", "The interactive Land & Power map for your parcel"),
    "compare_sites":             ("the full side-by-side scorecard across every layer for your shortlist", "The comparative map view"),
    "site_selection_canvas":     ("the ranked shortlist with every layer scored against your criteria", "The interactive Site Selection Canvas"),
    "get_market_intel":          ("the complete market brief — capacity pipeline, DCPI, M&A and grid headroom", "The market dashboards + maps"),
    "get_market_dcpi_rank":      ("the full DC Hub Power Index breakdown for every market", "The DCPI map + rankings"),
    "rank_markets":              ("the full ranked market table across every DCPI layer", "The market ranking map"),
    "search_facilities":         ("the full facility set with power, operator, tenants and specs", "The facility map"),
    "get_facility":              ("the complete facility profile — power, tenants, specs and connectivity", "The facility map + layers"),
    "hyperscaler_deals":         ("the full deal tracker with values, structures and counterparties", "The deal map + timeline"),
    "get_interconnection_queue": ("the full live interconnection queue — projects, MW, status and time-to-power", "The queue map + load curves"),
    "get_gas_intelligence":      ("the full gas-suitability index — pipelines, capacity and economics for your site", "The gas pipeline + DCGI map"),
    "get_grid_scoreboard":       ("the full ranked grid scoreboard — fuel mix, headroom and DCPI across every ISO", "The live ISO scoreboard + maps"),
}
_TOOL_VALUE_DEFAULT = ("the complete result set this tool returns", "The interactive maps and layers")


def _render_form(tool: str, count: int, err: str = "") -> str:
    err_html = (f'<div class="err">{_esc(err)}</div>') if err else ""
    _val, _blind = _TOOL_VALUE.get((tool or "").strip(), _TOOL_VALUE_DEFAULT)
    # qa-0704: Stripe drops client_reference_id values outside [A-Za-z0-9_-]
    # SILENTLY — the old mcp%3Atool%3D...%3Aref%3Dclaim shape never survived
    # checkout, so a claim-page pack purchase could never be attributed. Use
    # the phase17 ref_<src>__tool_<name> shape (main.py:14033 parses it) with
    # a Stripe-legal sanitized tool name.
    _tool_ref = re.sub(r"[^A-Za-z0-9_-]", "-", (tool or "unknown").strip() or "unknown")
    return (_CLAIM_FORM_HTML
            .replace("__TOOL_REF__", _tool_ref)
            .replace("__TOOL__", _esc(tool or "this tool"))
            .replace("__COUNT__", str(count) if count else "several")
            .replace("__VALUE__", _esc(_val))
            .replace("__BLINDSPOT__", _esc(_blind))
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
    # Shell #44 audience separation: a human-view token pasted into /claim is
    # a human holding the right artifact at the wrong door — send them to
    # their own page instead of the single-use flow.
    if (payload.get("kind") or KIND_MCP_SESSION) == KIND_HUMAN_VIEW:
        return redirect(f"/relay/{token}", code=302)
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
        # r-claim-page-open (2026-06-24): stamp the FIRST open of this claim page
        # (COALESCE so repeat opens don't overwrite). This is the GET = "human
        # opened the page" signal, distinct from POST = "submitted email". Never
        # block form render on a write error (conn is autocommit).
        try:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE mcp_high_intent_sessions "
                    "SET claim_page_opened_at = COALESCE(claim_page_opened_at, NOW()) "
                    "WHERE mcp_session_id = %s AND tool_name = %s", (sid, tool))
        except Exception:
            note_swallowed_write("mcp_high_intent_sessions", where="mcp_high_intent_claim.claim_form")
            pass
        count = int(row[0] or 0)
        used_at = row[2]
        if used_at is not None:
            # r-graceful-machine-burn (2026-07-15): claim_used_at is set by BOTH a
            # genuine human form-submit (claim_email IS NOT NULL) AND the by-design
            # server.mjs machine auto-redeem, which burns the single-use token ~1s
            # after mint (claim_email IS NULL). A human who later clicks the relayed
            # URL in the machine case double-used nothing — don't dead-end them on a
            # bare 410; forward them to signup for their own free key. Keep the 410
            # only for a real human re-use, and fail toward it on any DB error.
            claimed_by_human = True
            _machine_key = None
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT claim_email, minted_api_key FROM mcp_high_intent_sessions "
                        "WHERE mcp_session_id = %s AND tool_name = %s", (sid, tool))
                    _r = cur.fetchone()
                claimed_by_human = bool(_r and _r[0])
                _machine_key = (_r[1] if _r and len(_r) > 1 else None)
            except Exception:
                claimed_by_human = True
            if not claimed_by_human:
                # r-durable-handoff (2026-07-18): this is the ONE relayed URL a
                # human ever actually clicks (4 opens/30d, all machine-burned by
                # then). Land them on the STABLE per-key upgrade page — usage
                # evidence + one-click key-bound checkout — instead of a generic
                # signup form that loses all context. Fail-soft to signup.
                try:
                    if _machine_key:
                        from routes.upgrade_handoff import build_upgrade_url
                        _upg = build_upgrade_url(_machine_key)
                        if _upg:
                            return redirect(_upg + "?src=claim_relay", code=302)
                except Exception as _uhe:
                    logger.debug("[claim_form] upgrade-handoff redirect skipped: %s", _uhe)
                return redirect("https://dchub.cloud/signup?src=claim_relay", code=302)
            body = _render_form(tool, count,
                                err="This claim was already used. "
                                    "Get another key at dchub.cloud/signup.")
            return Response(body, status=410, mimetype="text/html")
        return Response(_render_form(tool, count), status=200, mimetype="text/html")
    finally:
        try: c.close()
        except Exception: pass


# AUTO-REPAIR: duplicate route '/claim/<token>' also in routes/mcp_high_intent_claim.py:923 — review and remove one
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
    # Shell #44 audience separation: the human-view token binds nothing, ever.
    if (payload.get("kind") or KIND_MCP_SESSION) == KIND_HUMAN_VIEW:
        return redirect(f"/relay/{token}", code=302)
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
                           VALUES (%s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING + INTERVAL '7 days', %s, %s)
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

        # r-lead-notify (2026-07-04): a CAPTURED EMAIL is the real, sellable lead.
        # Ping the operator HERE (not at mint, which fires for anonymous rotating
        # sessions with no human). Fire-and-forget; never breaks the conversion.
        try:
            from routes.claim_notify import notify_operator_of_lead
            notify_operator_of_lead(email, tool, sid, count=int(row[0] or 0))
        except Exception as _ln:
            logger.debug("[claim_submit] lead-notify skipped: %s", _ln)

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


# ── GET /relay/<token> — the HUMAN-audience view (shell #44) ─────────
# Multi-open, binds NOTHING, 7-day TTL. This page's only jobs: tell the
# human what their agent was doing, whether a trial key is already carried,
# and where the existing self-serve surfaces are (/signup, /pricing). No
# form here on purpose — the STOP on bind-UX spend stands; this is
# instrument repair, and the instrument is the open-stamp.

_RELAY_VIEW_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Your AI agent hit a depth limit — DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<style>
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
       max-width:560px;margin:0 auto;padding:3rem 1.5rem 6rem;color:#e6e9f5;
       background:rgb(5,8,16);line-height:1.6}
  h1{font-size:1.6rem;margin:0 0 .5rem;font-weight:700;letter-spacing:-.02em;
     color:#fff;line-height:1.25}
  .sub{color:#9aa3bd;margin:0 0 1.5rem;font-size:.98rem}
  .badge{display:inline-flex;align-items:center;gap:8px;background:rgba(34,211,238,.10);
         color:#22d3ee;padding:6px 14px;border-radius:999px;font-size:.72rem;
         font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin-bottom:1.25rem}
  .ctx{background:rgba(34,211,238,.06);border:1px solid rgba(34,211,238,.18);
       border-radius:10px;padding:16px 18px;margin:0 0 1.25rem;font-size:.9rem;color:#cbd5ff}
  .ctx strong{color:#22d3ee;font-weight:600}
  a.btn{display:block;text-align:center;background:linear-gradient(135deg,#22d3ee,#a855f7);
        color:#0a0f1f;padding:14px 24px;border-radius:10px;font-size:1rem;font-weight:700;
        text-decoration:none;margin:0 0 12px}
  a.ghost{display:block;text-align:center;color:#cbd5ff;border:1.5px solid rgba(255,255,255,.18);
        padding:13px 24px;border-radius:10px;font-size:.95rem;text-decoration:none;margin:0 0 12px}
  .fine{color:#6a7390;font-size:.8rem;margin-top:1.5rem}
  .fine a{color:#9aa3bd}
</style>
</head>
<body>
  <div class="badge">DC Hub · agent handoff</div>
  <h1>Your AI agent hit a depth limit__TOOL_SUFFIX__.</h1>
  <p class="sub">It was doing real work — __CONTEXT__ — and the deeper read is
  key-gated. This page is yours, not the agent's: safe to open any time this
  week, and opening it changes nothing on your account.</p>
  __KEY_STATE__
  <a class="btn" href="https://dchub.cloud/signup?src=relay">Get a free dev key (30 seconds)</a>
  <a class="ghost" href="https://dchub.cloud/pricing?src=relay">See paid depth &amp; pricing</a>
  <a class="ghost" href="https://dchub.cloud/mcp">What agents can query</a>
  <p class="fine">Why you're here: your agent asked us for a link a human could
  act on. The agent's own single-use key link is separate and stays machine-only.
  Questions: <a href="https://dchub.cloud/about">dchub.cloud/about</a></p>
</body>
</html>"""


def _render_relay_view(tool: str, count: int, agent_has_key: bool) -> str:
    tool_suffix = f" on <em>{tool}</em>" if tool else ""
    ctx = (f"<strong>{count} gated calls in 24h</strong> on this workflow"
           if count else "repeated gated calls on this workflow")
    key_state = (
        '<div class="ctx">Your agent already carries a <strong>7-day trial key'
        '</strong> (auto-issued so its work could continue). A key of your own '
        'outlives the trial and works across every agent you run.</div>'
        if agent_has_key else
        '<div class="ctx">No key is attached yet — the free dev key below takes '
        'about 30 seconds and works immediately in your agent.</div>')
    return (_RELAY_VIEW_HTML
            .replace("__TOOL_SUFFIX__", tool_suffix)
            .replace("__CONTEXT__", ctx)
            .replace("__KEY_STATE__", key_state))


@mcp_high_intent_claim_bp.route("/relay/<token>", methods=["GET"])
def relay_view(token: str):
    """Human-audience only (KIND_HUMAN_VIEW; agent/legacy kinds bounce to
    /claim). Stamps human_view_first_opened_at once and counts every open;
    since 2026-08-16 also stamps human_view_first_ua on the first open whose
    UA passes the canonical real-UA families — the funnel's human_acted v3
    reads THAT, so our own probes can render this page without moving the
    dashboard. Never cacheable: the page is per-token and the open IS the
    measurement."""
    payload = verify_human_view_token(token)
    if not payload:
        # A claim token pasted here? Send it to its own door.
        legacy = verify_claim_token(token)
        if legacy and (legacy.get("kind") or KIND_MCP_SESSION) != KIND_HUMAN_VIEW:
            return redirect(f"/claim/{token}", code=302)
        body = _render_relay_view("", 0, False).replace(
            "hit a depth limit", "sent a link that has expired")
        return Response(body, status=410, mimetype="text/html",
                        headers={"Cache-Control": "private, no-store"})
    sid, tool = payload["session_id"], payload["tool"]
    count, agent_has_key = 0, False
    c = _conn()
    if c is not None:
        try:
            _ensure_schema(c)
            ua = (request.headers.get("User-Agent") or "")[:300]
            with c.cursor() as cur:
                # First REAL-UA open wins the human_view_first_ua slot: the
                # predicate runs on the bound value, so a probe open (curl,
                # render-verify, …) increments the counters but leaves the
                # slot NULL for a later real open to claim. Single source:
                # mcp_calls_deloop.real_ua_predicate (no literal %, so it is
                # safe next to bound params — its own docstring pins that).
                cur.execute(
                    """UPDATE mcp_high_intent_sessions
                          SET human_view_first_opened_at =
                                  COALESCE(human_view_first_opened_at, NOW()),
                              human_view_opens = COALESCE(human_view_opens, 0) + 1,
                              human_view_first_ua = CASE
                                  WHEN human_view_first_ua IS NULL
                                       AND """ + real_ua_predicate("%s") + """
                                  THEN %s ELSE human_view_first_ua END
                        WHERE mcp_session_id = %s AND tool_name = %s
                        RETURNING paid_call_count_24h, claim_used_at""",
                    (ua, ua, sid, tool),
                )
                row = cur.fetchone()
                if row:
                    count = int(row[0] or 0)
                    agent_has_key = row[1] is not None
        except Exception as e:
            logger.warning("[relay_view] stamp failed: %s", str(e)[:150])
        finally:
            try: c.close()
            except Exception: pass
    return Response(_render_relay_view(tool, count, agent_has_key),
                    status=200, mimetype="text/html",
                    headers={"Cache-Control": "private, no-store"})


# ── POST /api/v1/mcp/high-intent/redeem ──────────────────────────────
# r-agent-redeem (2026-06-24): the AGENT-COMPLETABLE twin of the human
# POST /claim/<token> form. That form is the 95.8%-drop cliff — 24 claims
# minted in 30d, ~1 human ever opened the page (no human, no browser behind
# most agents). This lets the AGENT redeem the claim_token itself and bind the
# SAME 7d/50-call trial key, returning JSON it can save into its MCP config.
# Email is OPTIONAL here (the human-contact bridge becomes a secondary relay,
# not the gate). Security is IDENTICAL to the form — same HMAC
# verify_claim_token, same single-use (claim_used_at) idempotency, same per-IP
# rate-limit, same mint_trial_for_request grant — so it is no more abusable than
# the form that is already public. Token = body.token / ?token= / body.claim_token.

def _redeem_meta() -> dict:
    """The trial grant + next-step copy returned to a redeeming agent.
    r-capture (2026-06-25): identity + upgrade are the PRIMARY next steps now,
    not a footnote. A bare key with no email is dropped by header-less hosts
    (Claude.ai web, ChatGPT) — so it cannot be reused next session AND cannot be
    attributed to a conversion. Pushing bind_email turns this high-intent moment
    into a capture event instead of an anonymous giveaway."""
    return {
        "tier": "trial",
        "calls_total": 50,
        "expires_days": 7,
        "next_step": ("MAKE THIS DURABLE: call bind_email with your operator's email. "
                      "A bare key is dropped by header-less hosts (Claude.ai web, "
                      "ChatGPT) and cannot be reused next session — email/OAuth is the "
                      "only thing that persists. Want more than the 50-call trial? Call "
                      "unlock_more_data for a one-click checkout link to relay to your human."),
        "save_instruction": ("Also save this X-API-Key into your MCP client config "
                             "for the rest of this session."),
    }


def _handoff_fields(api_key: str | None) -> dict:
    """r-durable-handoff (2026-07-18): the auto-redeem response is the
    highest-traffic agent touchpoint this repo owns (1,566 machine redeems
    in 30d) — attach the STABLE per-key upgrade URL + an explicit relay
    instruction so the agent always holds a human-clickable, never-expiring
    link (the burned /claim token was the only URL it had before).
    Fail-soft: any error → {} so the redeem contract never breaks."""
    try:
        if not api_key:
            return {}
        from routes.upgrade_handoff import RELAY_INSTRUCTION, build_upgrade_url
        url = build_upgrade_url(api_key)
        if not url:
            return {}
        return {"human_upgrade_url": url,
                "relay_instruction": RELAY_INSTRUCTION}
    except Exception:
        return {}


@mcp_high_intent_claim_bp.route("/api/v1/mcp/high-intent/redeem", methods=["POST"])
def claim_redeem_agent():
    """Agent-side redemption of a high-intent claim token → binds the 7d/50-call
    trial key with NO human page-open. JSON in/out; email optional. Mirrors the
    security of POST /claim/<token> (see the block comment above)."""
    ip = _client_ip(request)
    # The trusted gateway (X-Internal-Key) auto-redeems for many agents from ONE
    # IP — exempt it from the per-IP brake (its volume is already bounded by the
    # mint-gate). Direct (un-keyed) agent calls stay rate-limited 10/hr/IP.
    if not _internal_ok(request) and not _rate_limit_ok(ip):
        return jsonify(ok=False, error="rate_limited",
                       hint="Too many redemptions from this IP; try again within the hour."), 429
    body = request.get_json(silent=True) or {}
    token = (request.args.get("token") or body.get("token")
             or body.get("claim_token") or "").strip()
    if not token:
        return jsonify(ok=False, error="missing_token"), 400
    payload = verify_claim_token(token)
    if not payload:
        return jsonify(ok=False, error="invalid_or_expired_token"), 400
    # Shell #44 audience separation: an agent (or the gateway's auto-redeem)
    # presenting the HUMAN link must never bind a key with it — that is the
    # exact single-token failure this split exists to end. 403, named.
    if (payload.get("kind") or KIND_MCP_SESSION) == KIND_HUMAN_VIEW:
        return jsonify(ok=False, error="audience_mismatch",
                       hint="human_url is view-only for your HUMAN; redeem "
                            "with claim_token/claim_url instead."), 403
    sid = payload["session_id"]
    tool = payload["tool"]
    # Email is OPTIONAL on this path; a malformed one is ignored, not rejected.
    email = (body.get("email") or "").strip().lower()
    if email and not _EMAIL_RE.match(email):
        email = ""
    # r-variant-honest-split (2026-07-11): OPTIONAL redemption-time variant
    # hint. Gateway-fronted sessions (mcp-remote → clientInfo.name='mcp',
    # UA='node') lock claim_variant='generic' at track/mint time even when
    # the actual host is Claude/Cursor/…, masking the platform A/B. When the
    # redeeming caller knows the real platform, it may send {variant:...} and
    # we UPGRADE a generic/NULL lock — a specific platform lock is never
    # overwritten (first SPECIFIC observation still wins).
    redeem_variant = (body.get("variant") or "").strip().lower()
    if redeem_variant not in VALID_VARIANTS or redeem_variant == "generic":
        redeem_variant = None

    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 503
    try:
        _ensure_schema(c)
        row = _lookup_claim_row(c, sid, tool)
        if not row:
            return jsonify(ok=False, error="claim_not_found_or_expired"), 410
        # Idempotent: already redeemed → return the existing key, never re-mint.
        if row[2] is not None:  # claim_used_at
            with c.cursor() as cur:
                cur.execute("SELECT minted_api_key FROM mcp_high_intent_sessions "
                            "WHERE mcp_session_id=%s AND tool_name=%s", (sid, tool))
                r = cur.fetchone() or (None,)
            if r[0]:
                return jsonify(ok=True, api_key=r[0], already_redeemed=True,
                               **_handoff_fields(r[0]), **_redeem_meta()), 200
            return jsonify(ok=False, error="already_used_no_key"), 410

        # Mint the trial key — SAME path as the human form.
        api_key = None
        mint_error = None
        try:
            from routes.auto_trial import mint_trial_for_request as _mint
            mr = _mint(req=request, tool_name=tool,
                       client_name="high-intent-agent-redeem",
                       operator_email=email)  # "" when absent — matches the str contract
            if isinstance(mr, dict) and mr.get("ok"):
                api_key = mr.get("api_key")
            else:
                mint_error = (mr or {}).get("error") if isinstance(mr, dict) else None
        except Exception as e:
            mint_error = f"{type(e).__name__}: {e}"
            logger.warning("[redeem-agent] mint failed: %s", e)
        if not api_key:
            # Same never-empty-handed fallback as the form.
            api_key = "dch_trial_" + secrets.token_urlsafe(24).replace("_", "x").replace("-", "x")[:32]
            try:
                with c.cursor() as cur:
                    cur.execute(
                        """INSERT INTO auto_trial_keys
                             (api_key, minted_for_tool, request_ip_hash, request_ua,
                              expires_at, operator_email, client_name)
                           VALUES (%s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING + INTERVAL '7 days', %s, %s)
                           ON CONFLICT (api_key) DO NOTHING""",
                        (api_key, tool, hashlib.sha256(ip.encode()).hexdigest()[:16],
                         (request.headers.get("User-Agent") or "")[:200],
                         (email or None), "high-intent-agent-redeem-fallback"))
            except Exception as e:
                logger.warning("[redeem-agent] fallback insert failed: %s", e)

        # Optional email — only if the agent passed one (secondary contact bridge).
        if email and api_key:
            try:
                from routes.redeem_routes import _p99_send_email as _send
                _send(email, api_key, [tool])
            except Exception as e:
                logger.warning("[redeem-agent] optional email failed: %s", e)

        # Mark used + persist — ATOMIC guard (claim_used_at IS NULL) so two
        # concurrent redeems of the same token can't both mint (TOCTOU). The
        # loser (rowcount 0) re-reads and returns the winner's already-bound key.
        won = True
        try:
            with c.cursor() as cur:
                cur.execute(
                    """UPDATE mcp_high_intent_sessions
                          SET claim_used_at = NOW(), claim_email = %s, minted_api_key = %s,
                              claim_variant = CASE
                                  WHEN %s IS NOT NULL
                                   AND COALESCE(NULLIF(claim_variant,''),'generic') = 'generic'
                                  THEN %s ELSE claim_variant END
                        WHERE mcp_session_id = %s AND tool_name = %s
                          AND claim_used_at IS NULL""",
                    ((email or None), api_key, redeem_variant, redeem_variant, sid, tool))
                won = (cur.rowcount == 1)
        except Exception as e:
            logger.warning("[redeem-agent] mark-used failed: %s", e)
        if not won:
            with c.cursor() as cur:
                cur.execute("SELECT minted_api_key FROM mcp_high_intent_sessions "
                            "WHERE mcp_session_id=%s AND tool_name=%s", (sid, tool))
                r = cur.fetchone() or (None,)
            if r[0]:
                logger.info("[redeem-agent] lost mint race; returning winner key tool=%s", tool)
                return jsonify(ok=True, api_key=r[0], already_redeemed=True,
                               **_handoff_fields(r[0]), **_redeem_meta()), 200

        # CRM bridge — only when the agent supplied an email (mirrors the form).
        if email:
            try:
                from routes.crm_reverse_etl import capture_event as _crm_capture
                _crm_capture("mcp_high_intent", {"email": email, "session_id": sid,
                                                 "tool": tool, "claim_token": token[:16],
                                                 "source": "agent_redeem"})
            except Exception as _e_crm:
                logger.debug("[redeem-agent] crm capture skipped: %s", _e_crm)
            # r-lead-notify (2026-07-04): agent bound an email → real contactable lead.
            try:
                from routes.claim_notify import notify_operator_of_lead
                notify_operator_of_lead(email, tool, sid, count=int(row[0] or 0))
            except Exception as _ln:
                logger.debug("[redeem-agent] lead-notify skipped: %s", _ln)

        logger.info("[redeem-agent] minted key=%s tool=%s email=%s mint_error=%s",
                    (api_key or "")[:16] + "...", tool, bool(email), mint_error)
        return jsonify(ok=True, api_key=api_key, already_redeemed=False,
                       **_handoff_fields(api_key), **_redeem_meta()), 200
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
       * claims_used_30d        — HUMAN-OPENED claims (see r-used-is-human below)
       * claims_redeemed_30d    — any-channel redeem (human OR machine auto-redeem)
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
        "claims_opened_30d": 0,
        "claims_redeemed_30d": 0,
        "claims_used_human_30d": 0,
        "claims_used_agent_30d": 0,
        "claim_minted_rate_30d_pct": 0.0,
        "claim_to_paid_rate_30d_pct": 0.0,
        "claim_to_paid_30d": 0,
        "claims_with_key_30d": 0,
        "claim_email_captured_30d": 0,
    }
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # r-claim-script-guard followup (2026-06-23): this PUBLIC stats route
            # was missed by f2c595bc (which applied _hi_real_sql to the mint gate,
            # the admin step-drop, and funnel_health). Without it these COUNT(*)s
            # reported the script/self-test-inflated 585/123 instead of the ~24
            # real pool, and brain_qa + press/dashboard surfaces re-ingested the
            # vanity number. Wrap the three counts with the SAME real-traffic
            # predicate (bare table → no prefix, like the step-drop base queries).
            # claim_to_paid below is left as-is: it JOINs on claim_email, which a
            # script never has, so it is already real-only.
            try:
                cur.execute(
                    """SELECT COUNT(*) FROM mcp_high_intent_sessions
                        WHERE last_hit_at >= NOW() - INTERVAL '30 days'
                          AND paid_call_count_24h >= %s"""
                    + " AND " + _hi_real_sql(),
                    (HIGH_INTENT_THRESHOLD,),
                )
                out["high_intent_sessions_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            try:
                cur.execute(
                    """SELECT COUNT(*) FROM mcp_high_intent_sessions
                        WHERE claim_minted_at IS NOT NULL
                          AND claim_minted_at >= NOW() - INTERVAL '30 days'"""
                    + " AND " + _hi_real_sql(),
                )
                out["claims_minted_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            # r-used-is-human (2026-07-27): claims_used_30d counted claim_used_at,
            # which since the 07-04 auto-redeem restore is DOMINATED by server.mjs
            # _autoRedeemClaim stamping every fresh mint ~0-25s later with
            # X-Internal-Key — no human, no browser. Live proof it was measuring the
            # machine: claims_used_30d(326) == claims_with_key_30d(326) exactly, and
            # _email_captured_sources.claim_flow == 0 (326 "used" claims, zero emails
            # from the claim form). Against a 3/30d baseline that read as a 100x
            # breakout and tripped the growth monitor daily.
            #
            # Same fix, same instrument as r-funnel-honest (2026-06-25), which already
            # repointed the dashboard's human_acted at claim_page_opened_at — this
            # PUBLIC route was missed by it, exactly like the 06-23 _hi_real_sql
            # followup missed it. claim_page_opened_at is stamped ONLY by the GET of
            # the HTML claim form (a browser rendered the page); the machine redeem
            # POSTs straight to /high-intent/redeem and never touches it.
            #
            # The machine number is NOT deleted — it moves to claims_redeemed_30d
            # (any channel) and splits into claims_used_agent_30d (claim_email IS
            # NULL = auto-redeem) / claims_used_human_30d (IS NOT NULL = human
            # form-submit), the Branch-A/B convention already used by
            # claim-variant-conversion. Consumers that want "did auto-redeem fire"
            # read claims_redeemed_30d; consumers that want humans read this key.
            try:
                # DISTINCT session — a single looping registry client (e.g. one
                # Smithery session crossing threshold on 10 tools) was inflating
                # this 10x via COUNT(*). Count real sessions, not rows.
                cur.execute(
                    """SELECT COUNT(DISTINCT mcp_session_id)
                         FROM mcp_high_intent_sessions
                        WHERE claim_page_opened_at IS NOT NULL
                          AND claim_page_opened_at >= NOW() - INTERVAL '30 days'"""
                    + " AND " + _hi_real_sql(),
                )
                out["claims_used_30d"] = int((cur.fetchone() or [0])[0] or 0)
                out["claims_opened_30d"] = out["claims_used_30d"]
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Any-channel redeem — the PREVIOUS claims_used_30d definition, kept
            # under an honest name so the auto-redeem health signal is not lost.
            try:
                cur.execute(
                    """SELECT COUNT(DISTINCT mcp_session_id)
                         FROM mcp_high_intent_sessions
                        WHERE claim_used_at IS NOT NULL
                          AND claim_used_at >= NOW() - INTERVAL '30 days'"""
                    + " AND " + _hi_real_sql(),
                )
                out["claims_redeemed_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Split that redeem total into its two channels.
            try:
                cur.execute(
                    """SELECT COUNT(DISTINCT mcp_session_id)
                           FILTER (WHERE claim_email IS NOT NULL AND claim_email <> ''),
                              COUNT(DISTINCT mcp_session_id)
                           FILTER (WHERE claim_email IS NULL OR claim_email = '')
                         FROM mcp_high_intent_sessions
                        WHERE claim_used_at IS NOT NULL
                          AND claim_used_at >= NOW() - INTERVAL '30 days'"""
                    + " AND " + _hi_real_sql(),
                )
                _r = cur.fetchone() or (0, 0)
                out["claims_used_human_30d"] = int(_r[0] or 0)
                out["claims_used_agent_30d"] = int(_r[1] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # r-loop-metric (2026-07-04): the TRUE Move #2 firing signal is not
            # claims_used alone but "did the agent walk away with a WORKING key" —
            # minted_api_key IS NOT NULL. And the 07-03 pivot's own success metric is
            # claim_email_captured (consented email → human follow-up pipeline).
            # Surfacing both lets the monitor watch the metric that now matters instead
            # of false-alarming on a deliberately-retired sub-path. Same real-traffic
            # guard, same fail-soft try/except as the counts above.
            try:
                cur.execute(
                    """SELECT COUNT(DISTINCT mcp_session_id)
                         FROM mcp_high_intent_sessions
                        WHERE minted_api_key IS NOT NULL
                          AND claim_used_at >= NOW() - INTERVAL '30 days'"""
                    + " AND " + _hi_real_sql(),
                )
                out["claims_with_key_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
            # r-email-reconcile (2026-07-10): claim_email_captured used to read ONLY
            # mcp_high_intent_sessions.claim_email — but the OPERATIVE bind path
            # (bind_email -> POST /auto-trial/bind) writes auto_trial_keys.operator_email
            # and /auto-trial/redeem writes signed_up_email, so this metric read 0 while
            # the auto-trial funnel correctly counted the binds (same bug class as the
            # auto_trial r78 signed_up-only fix). Reconcile to the CANONICAL UNION of
            # every email sink — claim_flow u operator_bind u signed_up u paid_conversion
            # — deduped by lower(email), 30d. _email_captured_sources keeps the per-sink
            # split (claim_flow preserves the old number). Fail-soft: seed from the
            # claim-path-only count so a wrong column in any sink can never regress <0.
            try:
                cur.execute(
                    """SELECT COUNT(DISTINCT LOWER(TRIM(claim_email)))
                         FROM mcp_high_intent_sessions
                        WHERE claim_email IS NOT NULL AND claim_email <> ''
                          AND claim_used_at >= NOW() - INTERVAL '30 days'"""
                    + " AND " + _hi_real_sql(),
                )
                out["claim_email_captured_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Union across all four email sinks (built once; reused for the deduped
            # total + the per-source breakdown). claim_flow keeps the real-traffic
            # guard; the other sinks are inherently real (an email was supplied).
            _email_sink_union = (
                "SELECT 'claim_flow' AS src, LOWER(TRIM(claim_email)) AS email "
                "  FROM mcp_high_intent_sessions "
                " WHERE claim_email IS NOT NULL AND claim_email <> '' "
                "   AND claim_used_at >= NOW() - INTERVAL '30 days' AND " + _hi_real_sql() + " "
                "UNION ALL "
                "SELECT 'operator_bind', LOWER(TRIM(operator_email)) "
                "  FROM auto_trial_keys "
                " WHERE operator_email IS NOT NULL AND operator_email <> '' "
                "   AND COALESCE(last_used_at, minted_at) >= NOW() - INTERVAL '30 days' "
                "UNION ALL "
                "SELECT 'signed_up', LOWER(TRIM(signed_up_email)) "
                "  FROM auto_trial_keys "
                " WHERE signed_up_email IS NOT NULL AND signed_up_email <> '' "
                "   AND COALESCE(last_used_at, minted_at) >= NOW() - INTERVAL '30 days' "
                "UNION ALL "
                "SELECT 'paid_conversion', LOWER(TRIM(user_email)) "
                "  FROM mcp_conversions "
                " WHERE user_email IS NOT NULL AND user_email <> '' "
                "   AND created_at >= NOW() - INTERVAL '30 days' "
                # 2026-07-10 (deep-dive audit): 5th sink — dch_live_ keys bound via
                # /api/v1/keys/identify write mcp_dev_keys.email; header-less binds
                # never reach mcp_high_intent_sessions.claim_email, so they were
                # invisible to this union.
                "UNION ALL "
                "SELECT 'dev_key_bind', LOWER(TRIM(email)) "
                "  FROM mcp_dev_keys "
                " WHERE email IS NOT NULL AND email <> '' "
                "   AND COALESCE(last_used_at, created_at) >= NOW() - INTERVAL '30 days' "
            )
            try:
                cur.execute("SELECT COUNT(DISTINCT email) FROM (" + _email_sink_union
                            + ") u WHERE email IS NOT NULL AND email <> ''")
                out["claim_email_captured_30d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            try:
                cur.execute("SELECT src, COUNT(DISTINCT email) FROM (" + _email_sink_union
                            + ") u WHERE email <> '' GROUP BY src")
                _by = {r[0]: int(r[1] or 0) for r in (cur.fetchall() or [])}
                out["_email_captured_sources"] = {
                    k: _by.get(k, 0) for k in
                    ("claim_flow", "operator_bind", "signed_up", "paid_conversion")}
            except Exception:
                try: c.rollback()
                except Exception: pass
            # claim → paid conversion (DISTINCT session). Attribute on the captured
            # claim_email landing in the canonical mcp_conversions table after the
            # redeem. NOTE: this honestly reads ~0 until the redeem actually captures
            # an email (see _redeem_meta r-capture) — the agent-redeem path stored
            # NULL email, so anonymous redemptions are correctly un-attributable, not
            # faked. As bind_email capture lands, this starts tracking real conversions.
            try:
                cur.execute(
                    """SELECT COUNT(DISTINCT h.mcp_session_id)
                         FROM mcp_high_intent_sessions h
                         JOIN mcp_conversions mc ON LOWER(mc.user_email) = LOWER(h.claim_email)
                        WHERE h.claim_used_at IS NOT NULL
                          AND h.claim_used_at >= NOW() - INTERVAL '30 days'
                          AND h.claim_email IS NOT NULL AND h.claim_email <> ''
                          AND mc.created_at >= h.claim_used_at - INTERVAL '7 days'
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
        # r-used-is-human (2026-07-27): denominator is the HUMAN form-submit cohort,
        # not every redeem. claim_to_paid_30d requires claim_email IS NOT NULL (it
        # JOINs mcp_conversions on that email), so it is a strict subset of
        # claims_used_human_30d — the old denominator (all redeems, ~99% machine)
        # made the numerator structurally incapable of filling it.
        if out["claims_used_human_30d"] > 0:
            out["claim_to_paid_rate_30d_pct"] = round(
                100.0 * out["claim_to_paid_30d"] / out["claims_used_human_30d"], 2)
        out["_claims_used_definition"] = (
            "claims_used_30d = DISTINCT real sessions with claim_page_opened_at in 30d "
            "(HUMAN opened the /claim/<token> page). Changed 2026-07-27 (r-used-is-human); "
            "before that it counted claim_used_at, which is ~99% server-side machine "
            "auto-redeem. Old number = claims_redeemed_30d; channel split = "
            "claims_used_human_30d (form-submit) / claims_used_agent_30d (auto-redeem)."
        )
        return jsonify(ok=True, **out, threshold=HIGH_INTENT_THRESHOLD)
    finally:
        try: c.close()
        except Exception: pass


# ── GET /api/v1/admin/mcp/claim-variant-conversion ────────────────────
# Round 2 (2026-06-07): per-platform A/B reporting. Surfaces:
#   minted (claim URLs sent) → used (claim token redeemed, ANY channel)
#                            → paid (joined to users.email + plan != free)
# Window: ?days=N (default 30, max 365). Admin-keyed.
#
# r-variant-honest-split (2026-07-11): `used` alone was a cross-cohort LIE.
# Since r-agent-redeem was restored (07-04), server.mjs machine-auto-redeems
# every fresh mint ~0-25s later with X-Internal-Key — no human, no browser.
# That stamped 'generic' (gateway/mcp-remote agents, clientInfo.name='mcp')
# at 99.3% "used" while 'claude' sat at 0% — but ALL 8 live claude mints
# (06-11→06-22) PREDATE the auto-redeem restore, and Claude.ai/desktop are
# header-less hosts whose only redemption path is a HUMAN clicking
# /claim/<token> (0 human email submissions in 30d). The 99.3%-vs-0% gap
# measured "was machine auto-redeem live at mint time", not copy quality.
# So: split used → used_agent (claim_email IS NULL = machine auto-redeem,
# Branch-A convention) vs used_human (claim_email IS NOT NULL = human
# form-submit, Branch B), plus opened (claim_page_opened_at) — the copy A/B
# signal is used_human/opened, never the machine channel.


def _variant_conversion_row(variant: str, minted: int, used_agent: int,
                            used_human: int, opened: int, paid: int) -> dict:
    """Pure builder for one per-variant A/B row. `used` stays the sum of both
    redemption channels for backward compatibility, but the copy-A/B signal
    is the HUMAN side (human_use_rate_pct / opened) — used_agent is the
    server-side machine auto-redeem and says nothing about the copy."""
    minted = int(minted or 0)
    used_agent = int(used_agent or 0)
    used_human = int(used_human or 0)
    opened = int(opened or 0)
    paid = int(paid or 0)
    used = used_agent + used_human
    return {
        "variant": variant or "generic",
        "minted": minted,
        "used": used,
        "used_agent": used_agent,
        "used_human": used_human,
        "opened": opened,
        "paid": paid,
        "use_rate_pct": round(100.0 * used / minted, 2) if minted else 0.0,
        "human_use_rate_pct": round(100.0 * used_human / minted, 2) if minted else 0.0,
        "paid_rate_pct": round(100.0 * paid / minted, 2) if minted else 0.0,
    }

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
                                         AND h.claim_minted_at >= NOW() - %s * INTERVAL '1 day') AS minted,
                        COUNT(*) FILTER (WHERE h.claim_used_at IS NOT NULL
                                         AND h.claim_used_at   >= NOW() - %s * INTERVAL '1 day'
                                         AND h.claim_email IS NULL) AS used_agent,
                        COUNT(*) FILTER (WHERE h.claim_used_at IS NOT NULL
                                         AND h.claim_used_at   >= NOW() - %s * INTERVAL '1 day'
                                         AND h.claim_email IS NOT NULL) AS used_human,
                        COUNT(*) FILTER (WHERE h.claim_page_opened_at IS NOT NULL
                                         AND h.claim_page_opened_at >= NOW() - %s * INTERVAL '1 day') AS opened,
                        COUNT(DISTINCT CASE
                            WHEN h.claim_used_at IS NOT NULL
                                 AND h.claim_used_at >= NOW() - %s * INTERVAL '1 day'
                                 AND u.email IS NOT NULL
                                 AND COALESCE(u.plan,'free') NOT IN ('free','')
                            THEN LOWER(h.claim_email)
                        END) AS paid
                    FROM mcp_high_intent_sessions h
                    LEFT JOIN users u ON LOWER(u.email) = LOWER(h.claim_email)
                    GROUP BY 1
                    -- ordinals: PG forbids expressions over output aliases in
                    -- ORDER BY (used_agent + used_human would error).
                    ORDER BY 2 DESC, 3 DESC, 4 DESC, 6 DESC
                    """ % (days, days, days, days, days)
                )
                for r in cur.fetchall():
                    row = _variant_conversion_row(
                        r[0] or "generic", r[1], r[2], r[3], r[4], r[5])
                    # Only include variants that have actually been minted —
                    # an empty 'generic' row from pre-Round-2 data is fine,
                    # but variants that have NEVER fired are skipped to keep
                    # the dashboard signal-to-noise high.
                    if row["minted"] == 0 and row["used"] == 0 and row["paid"] == 0:
                        continue
                    rows.append(row)
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
                rows.append(_variant_conversion_row(v, 0, 0, 0, 0, 0))

        # Totals across all variants (handy for the dashboard header).
        total_minted     = sum(r["minted"]     for r in rows)
        total_used       = sum(r["used"]       for r in rows)
        total_used_agent = sum(r["used_agent"] for r in rows)
        total_used_human = sum(r["used_human"] for r in rows)
        total_opened     = sum(r["opened"]     for r in rows)
        total_paid       = sum(r["paid"]       for r in rows)
        return jsonify(
            ok=True,
            window_days=days,
            variants=rows,
            totals={
                "minted": total_minted,
                "used": total_used,
                "used_agent": total_used_agent,
                "used_human": total_used_human,
                "opened": total_opened,
                "paid": total_paid,
                "use_rate_pct": round(100.0 * total_used / total_minted, 2) if total_minted else 0.0,
                "human_use_rate_pct": round(100.0 * total_used_human / total_minted, 2) if total_minted else 0.0,
                "paid_rate_pct": round(100.0 * total_paid / total_minted, 2) if total_minted else 0.0,
            },
            note=("used_agent = server-side machine auto-redeem (server.mjs "
                  "_autoRedeemClaim, no human involved — restored 2026-07-04); "
                  "used_human = human email form-submit on /claim/<token>; "
                  "opened = human loaded the claim page. Copy A/B verdicts must "
                  "read used_human/opened — comparing raw `used` across variants "
                  "compares machine-capable cohorts against human-click cohorts."),
            threshold=HIGH_INTENT_THRESHOLD,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        try: c.close()
        except Exception: pass


# ── Two-branch waterfall builder ──────────────────────────────────────
# r-two-branch (2026-07-03): THE shared builder for both the
# /admin/funnel-health card and /api/v1/admin/mcp/high-intent/step-drop —
# one definition, no drift (the 06-27 r-claim-honest fix had to be applied
# twice because these two surfaces each had their own copy).
#
# The old 7-step LINEAR chain interleaved two incompatible populations:
# agents auto-redeem the claim token server-side with NO email
# (claim_email NULL — 11 of 12 live redemptions), humans form-submit WITH
# one. Email-keyed steps sat "above" key-keyed steps, so the card rendered
# Email 1 → Trial key 12 as a "-1100% drop" and step_drop_alarm pinned
# True on a math artifact the brain's 15-min monitor screamed about every
# cycle (07-02 + 07-03 flywheel QA). New shape:
#
#   TRUNK   paywall_sessions → claims_minted → claim_redeemed
#   AGENT   (claim_email IS NULL)   base → key_issued → first_api_call
#           — ALL in DISTINCT-minted-key units (r-distinct-keys 2026-07-03:
#             the gate re-issues one key across sessions; rows-vs-distinct
#             faked an 81.8% key→first_call "leak" that was really ~0%)
#           → agent_upsell (unlock_more_data called WITH that key —
#             mcp_call_log.api_key, no email involved)
#           → agent_paid   (Stripe pack/top-up on the SAME key —
#             mcp_topups.api_key_hash = sha256(key)[:32])
#   HUMAN   (claim_email IS NOT NULL)   base → key_issued
#           → redeem_url_viewed (email-keyed signal) → paid (users.plan)
#
# drop_from_prev is computed ONLY within a chain (a branch base is a SPLIT
# of claim_redeemed, not a drop → None) and clamped to [0, 100].
#
# The alarm is a BREAKAGE detector, not a conversion complaint: only
# `mechanical` transitions (mint→redeem, base→key_issued, key→first_call)
# with prev >= 5 can trip it. Upsell/paid are intent outcomes — 0 there is
# the growth problem the north-star metric tracks, not a 15-min incident.

def build_step_waterfall(run, days: int = 30) -> dict:
    """Compute the two-branch high-intent claim waterfall.

    run: callable(sql: str) -> int — a fail-soft scalar COUNT executor
    supplied by the caller (each surface brings its own cursor/rollback
    handling). Returns a dict with steps, alarm, killer + branch summaries.
    """
    d = max(1, min(365, int(days)))
    real   = _hi_real_sql()
    real_h = _hi_real_sql("h.")
    W    = f"NOW() - INTERVAL '{d} days'"
    base = f"SELECT COUNT(*) FROM mcp_high_intent_sessions WHERE {real} AND "

    # ── TRUNK ──────────────────────────────────────────────────────────
    n_hit    = run(base + f"last_hit_at >= {W}")
    n_minted = run(base + f"claim_minted_at IS NOT NULL AND claim_minted_at >= {W}")
    n_used   = run(base + f"claim_used_at IS NOT NULL AND claim_used_at >= {W}")
    # Human /claim page opens — diagnostic only (agents redeem with no
    # browser, so ~0 here is by design — see r-claim-honest 2026-06-27).
    n_page   = run(base + f"claim_page_opened_at IS NOT NULL AND claim_page_opened_at >= {W}")

    # ── BRANCH A: agent auto-redeem (no human email on the row) ────────
    # r-distinct-keys (2026-07-03): every step in this chain counts DISTINCT
    # minted keys, not session ROWS. The mint gate re-issues the SAME key when
    # one agent redeems across several sessions, so counting key_issued as
    # rows against a DISTINCT-key first_call faked an "81.8% leak" at
    # key→first_call (the 07-03 deep dive traced the whole Branch-A drop to
    # this unit mismatch — like-for-like the drop is ~0%). Keyless
    # redemptions can't dedup by key, so the base adds them as single events;
    # base→key_issued then reads as exactly the true mint breakage. Raw
    # redemption EVENTS are kept as a diagnostic in branch_agent — because of
    # the dedup, branch bases no longer necessarily sum to claim_redeemed.
    a_where  = f"claim_used_at IS NOT NULL AND claim_used_at >= {W} AND claim_email IS NULL"
    a_events = run(base + a_where)          # raw redemption rows (diagnostic)
    dbase    = ("SELECT COUNT(DISTINCT minted_api_key) "
                f"FROM mcp_high_intent_sessions WHERE {real} AND ")
    a_key    = run(dbase + a_where + " AND minted_api_key IS NOT NULL")
    a_nokey  = run(base + a_where + " AND minted_api_key IS NULL")
    a_base   = a_key + a_nokey              # distinct agents (keys + keyless events)
    a_first  = run(
        "SELECT COUNT(DISTINCT h.minted_api_key) FROM mcp_high_intent_sessions h "
        "JOIN auto_trial_keys a ON a.api_key = h.minted_api_key "
        f"WHERE {real_h} AND h.claim_email IS NULL AND h.minted_api_key IS NOT NULL "
        f"AND h.claim_used_at >= {W} AND a.last_used_at IS NOT NULL")
    # Agent upsell: the minted trial key later called unlock_more_data (the
    # tool that hands the human checkout links). mcp_call_log rows carry the
    # caller's api_key (server.mjs trackToolCall → /api/v1/mcp/track), so
    # this is KEY-attributed — claim_email plays no part.
    a_upsell = run(
        "SELECT COUNT(DISTINCT h.minted_api_key) FROM mcp_high_intent_sessions h "
        "JOIN mcp_call_log l ON l.api_key = h.minted_api_key "
        f"WHERE {real_h} AND h.claim_email IS NULL AND h.minted_api_key IS NOT NULL "
        f"AND h.claim_used_at >= {W} "
        f"AND l.tool = 'unlock_more_data' AND l.timestamp >= {W}")
    # Agent click (2026-08-07): the relayed checkout link was actually OPENED
    # by a human. Until now this step did not exist and could not exist — the
    # relay handed out a direct buy.stripe.com URL, so the whole upsell→paid
    # gap was one unmeasured jump and "26 → 0 paid" could not distinguish "the
    # agent never showed a human the link" from "a human looked and declined".
    # /go/c/<token> (routes/checkout_click_tracker.py) stamps the click first,
    # then 302s to Stripe. Join on the ref the MCP server binds: pk-/k- carry
    # sha256 of the DURABLE key, keyless callers carry the bare session id —
    # so match either. NOT `mechanical`: a zero here is an intent outcome (the
    # thing we are trying to learn), never a breakage to alarm on.
    #
    # ★Reads 0 for clicks that predate this deploy — the row simply did not
    # exist before. Do not read early zeros as "nobody clicked"; read them as
    # "not yet measured" until the first non-zero.
    a_click  = run(
        "SELECT COUNT(DISTINCT h.minted_api_key) FROM mcp_high_intent_sessions h "
        "JOIN mcp_checkout_clicks cc ON ("
        "     cc.ref = h.mcp_session_id "
        "  OR cc.ref = 'pk-' || ENCODE(SHA256(CONVERT_TO(h.minted_api_key,'UTF8')),'hex') "
        "  OR cc.ref = 'k-'  || ENCODE(SHA256(CONVERT_TO(h.minted_api_key,'UTF8')),'hex')) "
        f"WHERE {real_h} AND h.claim_email IS NULL AND h.minted_api_key IS NOT NULL "
        f"AND h.claim_used_at >= {W} AND cc.sig_ok AND cc.clicked_at >= {W}")
    # Raw checkout-click volume (2026-08-25). a_click above is COHORT-SCOPED: it
    # counts only clicks whose ref joins a claim-flow session or key. That is the
    # right numerator for the agent branch — but mcp_checkout_clicks has exactly
    # ONE reader in this codebase (the join above), so a click from OUTSIDE the
    # cohort is invisible on every surface, and a_click=0 gets read as "no human
    # ever opens these links" when it can only ever mean "nobody in the claim
    # cohort did".
    #
    # Measured on prod 2026-08-25: 6 rows all-time, 3 signature-valid in-window,
    # of which exactly ONE is a real human (Windows browser, 08-13, $10 pack).
    # Its ref is EMPTY, so it joins nothing — server.mjs _goUrl embeds a
    # client_reference_id only when the caller had a durable key (pk-/k-) or an
    # Mcp-Session-Id, so a keyless AND sessionless caller (the hosted
    # header-less client class) receives a signed, payable, wholly
    # unattributable link. a_click read 0 while a human demonstrably clicked.
    #
    # DIAGNOSTICS, never steps: the denominator is all clicks, not the claim
    # cohort, so these must never feed drop_from_prev or the alarm. Script UAs
    # are excluded with the SAME canonical token list the session side uses, so
    # the curl QA probes that wrote 5 of the 6 rows cannot inflate them.
    _cc_real = f"COALESCE(cc.user_agent,'') !~* '{_SCRIPT_UA_SQL}'"
    a_click_all = run(
        "SELECT COUNT(*) FROM mcp_checkout_clicks cc "
        f"WHERE cc.sig_ok AND cc.clicked_at >= {W} AND {_cc_real}")
    a_click_unattr = run(
        "SELECT COUNT(*) FROM mcp_checkout_clicks cc "
        f"WHERE cc.sig_ok AND cc.clicked_at >= {W} AND {_cc_real} "
        "AND COALESCE(cc.ref,'') = ''")
    # Agent paid: a Stripe-backed pack/top-up bound to the SAME paywall session.
    # r-attr-sid (2026-07-06): the ORIGINAL join matched sha256(minted_api_key)
    # against mcp_topups.api_key_hash — but minted_api_key is always a dch_trial_
    # key while the pack webhook only ever hashes a dch_live_ key, so that leg can
    # NEVER match (verified: 0/0 on prod). The identifier that actually survives
    # end-to-end is the Mcp-Session-Id: the relayed $10 link carries
    # client_reference_id=<session> (server.mjs _stripeWithSession), and the pack
    # webhook stores it as mcp_topups.mcp_session_id (main.py grant_credit_pack).
    # Join on THAT (keeping the key-hash leg as a harmless fallback). Garbage refs
    # like 'ref_claim__tool_*' can't collide with a real session UUID. This is a
    # measurement-correctness fix — it does not manufacture conversions; it makes
    # a genuine relay conversion VISIBLE instead of structurally uncountable.
    a_paid   = run(
        "SELECT COUNT(DISTINCT h.mcp_session_id) FROM mcp_high_intent_sessions h "
        "JOIN mcp_topups t ON ("
        "     t.mcp_session_id = h.mcp_session_id "
        "  OR t.api_key_hash = LEFT(ENCODE(SHA256(CONVERT_TO(h.minted_api_key,'UTF8')),'hex'),32)) "
        f"WHERE {real_h} AND h.claim_email IS NULL AND h.mcp_session_id IS NOT NULL "
        f"AND h.claim_used_at >= {W} AND t.paid_at IS NOT NULL")

    # ── BRANCH B: human email form-submit ──────────────────────────────
    b_where  = f"claim_used_at IS NOT NULL AND claim_used_at >= {W} AND claim_email IS NOT NULL"
    b_base   = run(base + b_where)
    b_key    = run(base + b_where + " AND minted_api_key IS NOT NULL")
    b_view   = run(
        "SELECT COUNT(DISTINCT LOWER(h.claim_email)) FROM mcp_high_intent_sessions h "
        "JOIN mcp_upgrade_signals u ON LOWER(u.user_email) = LOWER(h.claim_email) "
        f"WHERE {real_h} AND h.claim_email IS NOT NULL "
        f"AND h.claim_used_at >= {W} AND u.signal_type = 'redeem_url_viewed'")
    b_paid   = run(
        "SELECT COUNT(DISTINCT LOWER(h.claim_email)) FROM mcp_high_intent_sessions h "
        "JOIN users u ON LOWER(u.email) = LOWER(h.claim_email) "
        f"WHERE {real_h} AND h.claim_email IS NOT NULL "
        f"AND h.claim_used_at >= {W} "
        "AND COALESCE(u.plan,'free') NOT IN ('free','')")

    def _clamp(x: float) -> float:
        return max(0.0, min(100.0, x))

    steps: list[dict] = []
    killer, killer_drop = "", -1.0

    def _add(name, label, branch, count, prev, mechanical):
        """prev=None → chain head or split point (no drop shown)."""
        nonlocal killer, killer_drop
        dfp = None
        if prev is not None and prev > 0:
            dfp = round(_clamp(100.0 * (prev - count) / prev), 2)
        cum = (round(_clamp(100.0 * (n_minted - count) / n_minted), 2)
               if (n_minted and name != "paywall_sessions") else None)
        steps.append({"step": name, "label": label, "branch": branch,
                      "count": count, "drop_from_prev": dfp,
                      "drop_pct": cum, "mechanical": mechanical})
        if (mechanical and dfp is not None and prev is not None
                and prev >= 5 and dfp > killer_drop):
            killer_drop = dfp
            killer = name

    _add("paywall_sessions", "Paywall-hit sessions (real)",  "trunk", n_hit,    None,     False)
    # paywall→minted is the mint GATE doing its job (threshold, dedup,
    # non-human exclusion) — show the number, never alarm on it.
    _add("claims_minted",    "Claim URL minted",              "trunk", n_minted, None,     False)
    _add("claim_redeemed",   "Claim redeemed (agent+human)",  "trunk", n_used,   n_minted, True)

    _add("agent_redeemed",   "Agent auto-redeems (distinct agents)",
                                                              "agent", a_base,   None,     False)
    _add("agent_key_issued", "Trial key issued (distinct keys)",
                                                              "agent", a_key,    a_base,   True)
    _add("agent_first_call", "Key made an API call (distinct keys)",
                                                              "agent", a_first,  a_key,    True)
    _add("agent_upsell",     "unlock_more_data checkout link (key-attributed)",
                                                              "agent", a_upsell, a_first,  False)
    _add("agent_click",      "Human OPENED the checkout link (/go/c)",
                                                              "agent", a_click,  a_upsell, False)
    _add("agent_paid",       "Paid pack/top-up on key (Stripe)",
                                                              "agent", a_paid,   a_click,  False)

    _add("human_redeemed",   "Human submitted email",         "human", b_base,   None,     False)
    _add("human_key_issued", "Trial key issued",              "human", b_key,    b_base,   True)
    _add("human_redeem_viewed", "Redeem/checkout URL viewed", "human", b_view,   b_key,    False)
    _add("human_paid",       "Paid plan (users.plan)",        "human", b_paid,   b_view,   False)

    # Alarm = BREAKAGE only (see block comment): dead mint gate, the 19/0/0
    # redeem stall, or a >95% mechanical drop off a prev >= 5.
    alarm = ((n_hit >= 5 and n_minted == 0)
             or (n_minted > 5 and n_used == 0)
             or killer_drop > 95.0)
    if killer_drop < 0:
        killer, killer_drop = "", 0.0

    return {
        "steps": steps,
        "alarm": alarm,
        "killer_step": killer,
        "killer_drop_pct": round(killer_drop, 2),
        "human_page_opens": n_page,
        "paywall_sessions": n_hit,
        "claims_minted": n_minted,
        "claims_redeemed": n_used,
        "paid_total": a_paid + b_paid,
        "branch_agent": {"base": a_base, "key_issued": a_key,
                         "first_api_call": a_first, "upsell": a_upsell,
                         "checkout_click": a_click,
                         # diagnostic (2026-08-25): every real checkout click in
                         # window, and the subset the cohort join CANNOT
                         # attribute. checkout_click=0 alongside a non-zero
                         # checkout_clicks_all means humans ARE opening the
                         # links and the funnel cannot see them — read the pair,
                         # never checkout_click alone.
                         "checkout_clicks_all": a_click_all,
                         "checkout_clicks_unattributed": a_click_unattr,
                         "paid": a_paid,
                         # diagnostic: raw redemption rows before the
                         # distinct-key dedup (r-distinct-keys 2026-07-03)
                         "redemption_events": a_events},
        "branch_human": {"base": b_base, "key_issued": b_key,
                         "redeem_url_viewed": b_view, "paid": b_paid},
    }


# ── GET /api/v1/admin/mcp/high-intent/step-drop ───────────────────────
# 2026-06-07 (restructured r-two-branch 2026-07-03): step-by-step drop-off
# monitor so the brain can scream the instant the claim funnel BREAKS —
# see build_step_waterfall above for the trunk + two-branch shape and the
# mechanical-only alarm semantics.
#
# All windows: ?days=N (default 30, max 365). Admin-keyed.

@mcp_high_intent_claim_bp.route(
    "/api/v1/admin/mcp/high-intent/step-drop", methods=["GET"])
def high_intent_step_drop():
    """Step-by-step drop-off monitor. The single page the brain checks
    every 15 min to scream the instant the claim funnel breaks.

    Returns:
      {ok, window_days,
       steps: [{step, label, branch, count, drop_from_prev, drop_pct,
                mechanical}, ...],
       overall_conversion_pct,
       killer_step: <biggest MECHANICAL drop_from_prev with prev >= 5>,
       alarm: bool,
       branch_agent / branch_human: per-branch summaries}
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

    try:
        _ensure_schema(c)
        # Fail-soft scalar executor — a missing column on a brand-new schema
        # can't blank the whole page (each probe returns 0 on error).
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

        wf = build_step_waterfall(lambda sql: _scalar(sql), days=days)
        n_minted = wf["claims_minted"]
        # Overall conversion = ALL paid outcomes (key-attributed agent packs +
        # email-attributed human plans) over claims minted.
        overall_conv = (round(100.0 * wf["paid_total"] / n_minted, 2)
                        if n_minted else 0.0)

        return jsonify(
            ok=True,
            window_days=days,
            steps=wf["steps"],
            overall_conversion_pct=overall_conv,
            killer_step=wf["killer_step"] or None,
            killer_drop_pct=wf["killer_drop_pct"],
            alarm=wf["alarm"],
            paywall_sessions=wf["paywall_sessions"],
            paid_total=wf["paid_total"],
            branch_agent=wf["branch_agent"],
            branch_human=wf["branch_human"],
            # r-claim-honest (2026-06-27): genuine human browser opens of /claim,
            # kept as a diagnostic (agents redeem server-side — ~0 by design).
            human_page_opens=wf["human_page_opens"],
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
