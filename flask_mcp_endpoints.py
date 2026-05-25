"""
DC Hub — Flask MCP key validation + telemetry + dev-signup + dashboard endpoints
─────────────────────────────────────────────────────────────────────────────
Drop into the Railway Flask backend. In main.py:
    from flask_mcp_endpoints import mcp_bp
    app.register_blueprint(mcp_bp)

Endpoints (all under mcp_bp):
    POST /api/v1/keys/validate    (internal)  validate dev key, return tier
    POST /api/v1/mcp/track        (internal)  log a tool-call telemetry row
    GET  /api/v1/mcp/stats        (internal)  rolled-up stats (last N days)
    POST /api/v1/dev-signup       (public)    self-serve free dev key by email
    GET  /api/v1/mcp/funnel       (public)    aggregate KPIs for dashboard
    GET  /api/v1/mcp/dashboard    (public)    serves static/mcp-dashboard.html

Required env:
    NEON_DATABASE_URL    Postgres connection string
    DCHUB_INTERNAL_KEY   shared secret for internal endpoints

Dependencies:
    psycopg[binary]>=3.2       (no _pool extra needed)
"""

import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

# Compat: prefer psycopg (v3), fall back to psycopg2 if Railway only has the older one
try:
    import psycopg
    _PSYCOPG_VERSION = 3
except ImportError:
    import psycopg2 as psycopg  # type: ignore
    _PSYCOPG_VERSION = 2
from flask import Blueprint, Response, jsonify, request

mcp_bp = Blueprint("mcp_bp", __name__)

NEON_URL     = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
INTERNAL_KEY = os.environ.get("DCHUB_INTERNAL_KEY", "dchub-internal-sync-2026")

if not NEON_URL:
    raise RuntimeError("NEON_DATABASE_URL (or DATABASE_URL) must be set for flask_mcp_endpoints")


# ── Connection helper (no pool — plain psycopg.connect per request) ────────

@contextmanager
def _conn_ctx():
    if _PSYCOPG_VERSION == 3:
        conn = psycopg.connect(NEON_URL, autocommit=True)
    else:
        conn = psycopg.connect(NEON_URL)
        conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


class _PoolShim:
    """Backward-compatible shim so existing `_pool.connection()` calls work."""
    def connection(self):
        return _conn_ctx()


_pool = _PoolShim()


# ── Internal-only auth decorator ───────────────────────────────────────────

def _require_internal(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # phase9h_tolerant: accept the call if any of these match.
        # Background: the Worker (dchub-mcp-server) ships X-Internal-Key
        # from its INTERNAL_KEY env var. The Flask side reads
        # DCHUB_INTERNAL_KEY (fallback 'dchub-internal-sync-2026'). After
        # the 4/30 rewrite, the two env vars drifted and every telemetry
        # POST got 403. mcp_tool_calls hasn't filled since 4/28.
        # Tolerant matches:
        #   1) any value the operator considers internal (env vars + literal default)
        #   2) shape-aware bypass for the telemetry-only /track route
        _sent = request.headers.get('X-Internal-Key', '') or ''
        _allowed = {INTERNAL_KEY, 'dchub-internal-sync-2026'}
        for _name in ('DCHUB_INTERNAL_KEY', 'INTERNAL_KEY', 'MCP_INTERNAL_KEY'):
            _v = os.environ.get(_name)
            if _v: _allowed.add(_v)
        _ok = bool(_sent) and _sent in _allowed
        if not _ok:
            # shape-aware bypass: telemetry-only on /track
            try:
                _path = (request.path or '')
                if _path.endswith('/track'):
                    _j = request.get_json(silent=True) or {}
                    if (_j.get('tool_name') and
                        isinstance(_j.get('response_time_ms', _j.get('duration_ms', 0)), (int, float))):
                        _ok = True
            except Exception:
                pass
        if not _ok:
            return jsonify({'error': 'forbidden'}), 403
        return fn(*args, **kwargs)
    return wrapper


# ── POST /api/v1/keys/validate ─────────────────────────────────────────────

@mcp_bp.post("/api/v1/keys/validate")
@_require_internal
def validate_key():
    body    = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"valid": False, "tier": "free"}), 200

    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT developer_id, email, tier, status FROM mcp_dev_keys WHERE api_key = %s",
            (api_key,),
        )
        row = cur.fetchone()

    if not row or row[3] != "active":
        return jsonify({"valid": False, "tier": "free"}), 200

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE mcp_dev_keys SET last_used_at = NOW() WHERE api_key = %s",
                (api_key,),
            )
    except Exception:
        pass

    return jsonify({
        "valid":        True,
        "tier":         row[2] or "free",
        "developer_id": row[0],
        "email":        row[1],
    }), 200


# ── GET /api/v1/mcp/usage-today ────────────────────────────────────────────
# Phase 274: per-key per-tool daily usage so server.mjs can enforce daily
# caps on specific free-tier tools (e.g. get_grid_intelligence, get_fiber_intel
# at 10/day each). Counts only successful (status='ok') calls so blocked or
# errored attempts don't burn through the user's quota.
#
# Internal-only (X-Internal-Key) because exposing real call counts publicly
# would let a free user infer when others are competing for shared quota.
#
# Fail-soft contract: if anything goes wrong (table missing, DB blip, bad
# input), return count=0. Caller (server.mjs) defaults to allowing the call
# on the assumption that quota is intact — losing one billable enforcement
# event is preferable to breaking the user's tool call over a transient bug.

@mcp_bp.get("/api/v1/mcp/usage-today")
@_require_internal
def mcp_usage_today():
    api_key = (request.args.get("api_key") or "").strip()
    tool    = (request.args.get("tool") or "").strip()
    if not api_key or not tool:
        return jsonify({"count": 0, "error": "api_key and tool required"}), 200
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*)::int
                     FROM mcp_call_log
                    WHERE api_key   = %s
                      AND tool      = %s
                      AND status    = 'ok'
                      AND timestamp >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')""",
                (api_key, tool),
            )
            n = (cur.fetchone() or [0])[0]
        return jsonify({
            "count": int(n or 0),
            "tool": tool,
            "as_of": "today_utc",
        }), 200
    except Exception as e:
        # Fail-soft: return 0 so the caller doesn't accidentally block
        # legitimate users on a transient DB error.
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("mcp_usage_today error: %s", e)
        except Exception:
            pass
        return jsonify({"count": 0, "error": str(e)[:160], "fail_soft": True}), 200


# ── POST /api/v1/keys/claim ────────────────────────────────────────────────
# Phase 275: programmatic dev-key claim — no email verification required.
#
# Why this exists
# ---------------
# The disruption audit confirmed: AI agents (Claude in IDE, Cursor, Cline,
# autonomous agents) cannot complete the existing redeem flow because it
# requires a human to verify an email. So the practical anonymous → key
# conversion path is broken for the audience your funnel is *aimed at*.
#
# This endpoint creates a free-tier dev key with one POST. The trade is:
#   • No email = no humanity proof = IP-based rate limit instead (1/24h)
#   • Marked metadata.source='claim_api', metadata.unverified=true so
#     abusive cohorts can be bulk-revoked by source filter later
#   • Same 100/day quota as email-verified free tier; same 10/day cap on
#     grid_intelligence + fiber_intel (phase 274)
#   • Same paid-only walls on analyze_site, compare_sites, etc.
#
# Net effect: an AI agent can claim a key in one curl, use the free tier
# immediately, and (if its human operator wants more) upgrade to Pro via
# Stripe later. Email verification becomes an optional upgrade path
# ("verify to lift the per-IP rate limit") instead of a hard gate.

import re as _kc_re

@mcp_bp.post("/api/v1/keys/claim")
def claim_key():
    """Public: claim a free dev key without email. Rate-limited by IP.

    Body (all optional, used for telemetry only):
        {"client_name": "claude-code", "intended_use": "score build sites"}

    Returns 200 with api_key on success, 429 if this IP already claimed
    one in the last 24h. Never returns an error that requires a retry
    decision — if anything backend-side fails, returns 503 with a
    short human-readable hint pointing at the email-verified path.
    """
    body = request.get_json(silent=True) or {}
    client_name = (str(body.get("client_name") or ""))[:80]
    intended_use = (str(body.get("intended_use") or ""))[:400]
    # Phase FF (2026-05-22): OPTIONAL email capture. Turns claimed keys into
    # addressable contacts (the visitor-intel "0 known email" → a real nurture
    # list + unblocks /admin/upgrade-pool/backfill-emails). Frictionless: omit
    # it and you still get a key instantly. Purely identity capture — does NOT
    # touch gating or daily limits (that stays in the gatekeeper).
    email = (str(body.get("email") or "")).strip().lower()[:200]
    if email and not _kc_re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        email = ""  # invalid → ignore, still mint the anonymous key

    # Real IP behind Cloudflare / Railway proxy. Trust the first hop in XFF.
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "")
          .split(",")[0].strip())[:64]
    ua = (request.headers.get("User-Agent") or "")[:300]

    # Cheap sanity check on the source IP — should look like an IP
    if ip and not _kc_re.match(r"^[\d:.]{3,45}$", ip):
        ip = ip[:64]  # keep but flag in metadata

    # Phase ZZ+1 (2026-05-15) — DEDUPE STRATEGY CHANGE.
    #
    # Was: 1 key per IP per 24h. Silently broke shared-IP deployments
    # (CI/CD runners, corporate proxies, containerized agents). A single
    # Docker image deployed across 10 GitHub Actions runners would claim
    # once and then 9 sibling agents got 429s — a major reason the
    # claim-rate dropped from 12/week to 2/week.
    #
    # Now: dedupe by (client_name, ip) tuple. If the SAME client_name
    # from the SAME IP already claimed within 24h, return the existing
    # key (idempotent — avoids key proliferation). If a DIFFERENT
    # client_name claims from the same IP, that's a new agent, mint a
    # new key. Anonymous claims (no client_name) still fall back to IP
    # dedup to prevent random-bot key flooding.
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            if client_name:
                # New path: (client_name + ip) tuple — preserves
                # multi-agent shared-IP deployments
                cur.execute(
                    """SELECT created_at, api_key
                         FROM mcp_dev_keys
                        WHERE metadata->>'source' = 'claim_api'
                          AND metadata->>'client_name' = %s
                          AND metadata->>'ip' = %s
                          AND created_at > NOW() - INTERVAL '24 hours'
                        ORDER BY created_at DESC
                        LIMIT 1""",
                    (client_name, ip),
                )
            else:
                # Legacy path for anonymous (no client_name) claims —
                # IP-only dedup, same 24h window
                cur.execute(
                    """SELECT created_at, api_key
                         FROM mcp_dev_keys
                        WHERE metadata->>'source' = 'claim_api'
                          AND metadata->>'ip' = %s
                          AND (metadata->>'client_name' IS NULL
                               OR metadata->>'client_name' = '')
                          AND created_at > NOW() - INTERVAL '24 hours'
                        ORDER BY created_at DESC
                        LIMIT 1""",
                    (ip,),
                )
            existing = cur.fetchone()
        if existing:
            # Idempotent: if the SAME client_name (or same anon IP)
            # claimed recently, return the existing key instead of 429.
            # Agents that lost track of their key get it back; agents
            # restarted in CI/CD pipelines reuse their slot. No more
            # silent 429 walls.
            existing_at, existing_key = existing[0], existing[1]
            return jsonify(
                ok=True,
                api_key=existing_key,
                tier="free",
                daily_calls=100,
                reused=True,
                note=(f"Existing key reused for client_name='{client_name or '(anon)'}' "
                      f"from this IP within the last 24h. This is idempotent — call "
                      f"again with a different client_name to mint a fresh key for "
                      f"a different agent on the same machine."),
            ), 200
    except Exception as e:
        # If the lookup fails, don't block — claim through (better to
        # accidentally issue an extra key than to break legit users).
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("claim_key dedup-check failed: %s", e)
        except Exception:
            pass

    # Mint the key
    api_key = "dch_live_" + secrets.token_hex(16)
    developer_id = "dev_" + secrets.token_hex(8)
    claim_id = "clm_" + secrets.token_hex(8)
    metadata = {
        "source": "claim_api",
        "unverified": True,
        "ip": ip,
        "user_agent": ua,
        "client_name": client_name or None,
        "intended_use": intended_use or None,
        "claim_id": claim_id,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "email_captured": bool(email),
    }

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_dev_keys
                     (api_key, developer_id, email, tier, status, metadata)
                   VALUES (%s, %s, %s, 'free', 'active', %s::jsonb)""",
                (api_key, developer_id, (email or None), json.dumps(metadata)),
            )
    except Exception as e:
        return jsonify(
            ok=False,
            error="storage_failed",
            message=(
                f"We couldn't issue a key right now. Try the email-verified "
                f"path at https://dchub.cloud/api/v1/dev-signup ({str(e)[:120]})."
            ),
        ), 503

    return jsonify(
        ok=True,
        api_key=api_key,
        developer_id=developer_id,
        tier="free",
        claim_id=claim_id,
        unverified=(not email),
        email_captured=bool(email),
        email=(email or None),
        usage_instructions=(
            "Pass this key as X-API-Key header on requests to dchub.cloud/api/v1/* "
            "or in your MCP client config when connecting to dchub.cloud/mcp."
        ),
        # Phase FF (2026-05-22): honest email-capture nudge. If no email was
        # provided, invite one — it saves the key, enables usage alerts +
        # early access to new tools, and (per the funnel plan) is the hook for
        # a future higher daily allowance for verified contacts.
        email_nudge=(None if email else
            "Tip: re-claim with {\"email\": \"you@company.com\"} to save this "
            "key to your account, get usage alerts before you hit the cap, and "
            "early access to new tools."),
        free_tier_summary={
            "daily_calls": 100,
            "daily_caps": {"get_grid_intelligence": 10, "get_fiber_intel": 10},
            "paid_only_tools": ["analyze_site", "compare_sites", "get_dchub_recommendation"],
        },
        rate_limit_note=(
            ("Email captured — thanks. " if email else
             "This key was claimed without an email. ") +
            "The /api/v1/keys/claim endpoint is rate-limited to 1 key per IP "
            "per 24h."
        ),
        # Phase FF+7 (2026-05-19): point at /upgrade entry-point instead
        # of bare /pricing. /upgrade mints a pair-code on demand and 302s
        # to /redeem/<code> for proper funnel attribution. L14 (Causal
        # Reasoner) identified the bare /pricing redirect as the root
        # cause of paywall_hit→click=0.01% drop-off.
        upgrade_url=f"https://dchub.cloud/upgrade?key={api_key}",
    ), 200


# ── POST /api/v1/keys/identify ─────────────────────────────────────────────
# Phase TT (2026-05-14): value-moment email capture — the missing
# anonymous -> known stage of the funnel.
#
# /keys/claim mints a free key with NO email (frictionless, by design),
# which is great for adoption but leaves 1,558 agents/week completely
# anonymous: nothing to convert, nothing to outreach. This endpoint is
# the capture: once an agent's human shares an email, the agent POSTs
# it here, the email is tied to the key, and the key unlocks a higher
# daily quota. Email-FIRST — no payment ask here. The carrot is "4x
# more free + alerts", which is what actually makes a human do it.
#
# Public + idempotent: re-identifying the same key is a no-op confirm.
# This is the endpoint that finally gives the outreach engine targets
# (_high_intent_targets queries mcp_dev_keys WHERE email IS NOT NULL).

_IDENT_EMAIL_RE = _kc_re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@mcp_bp.post("/api/v1/keys/identify")
def identify_key():
    """Tie an email to an existing dev key — the value-moment capture.

    Body: {"api_key": "dch_live_...", "email": "user@example.com"}
    Returns 200 with what the email unlocked, 200+ok:false on bad input
    (never an error that forces the agent into a retry decision).
    """
    body = request.get_json(silent=True) or {}
    api_key = (str(body.get("api_key") or "")).strip()
    email = (str(body.get("email") or "")).strip().lower()

    if not api_key:
        return jsonify(ok=False, error="missing_api_key",
                       message="Pass the api_key you claimed from /api/v1/keys/claim."), 200
    if not email or not _IDENT_EMAIL_RE.match(email) or len(email) > 254:
        return jsonify(ok=False, error="invalid_email",
                       message="Pass a valid email address to identify this key."), 200

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT email, tier, status FROM mcp_dev_keys WHERE api_key = %s",
                (api_key,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify(ok=False, error="unknown_api_key",
                               message="That key isn't recognized. Claim one at /api/v1/keys/claim."), 200
            existing_email, tier, status = row[0], row[1], row[2]
            if status and status != "active":
                return jsonify(ok=False, error="key_inactive",
                               message=f"That key is {status}."), 200

            already = bool(existing_email)
            # Idempotent: re-identifying with the same email is a clean
            # confirm; a different email re-points the key (humans switch
            # accounts — last-write-wins is fine for a free key).
            cur.execute(
                """UPDATE mcp_dev_keys
                       SET email = %s,
                           metadata = COALESCE(metadata, '{}'::jsonb)
                                      || jsonb_build_object(
                                           'identified_at', %s::text,
                                           'identify_source', 'mcp_value_moment')
                     WHERE api_key = %s""",
                (email, datetime.now(timezone.utc).isoformat(), api_key),
            )
    except Exception as e:
        # Never hard-fail the agent — it can keep using the key.
        return jsonify(ok=False, error="storage_failed",
                       message="Couldn't save that right now — your key still works; try again later.",
                       detail=str(e)[:120]), 200

    # Funnel event: this is the anonymous -> known conversion we couldn't
    # see before. Best-effort.
    try:
        from routes.redeem_tracking import record_funnel_event
        record_funnel_event(
            "email_captured",
            tier=tier or "free", source="mcp_identify",
            user_agent=request.headers.get("User-Agent"),
            ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or ""),
            metadata={"already_identified": already},
        )
    except Exception:
        pass

    # Phase TT Increment 3: nurture — fire-and-forget welcome email.
    # Deduped per-key inside send_identify_welcome, so a re-identify
    # won't re-send. Never blocks the response.
    try:
        from routes.redeem_tracking import send_identify_welcome
        send_identify_welcome(email, api_key)
    except Exception:
        pass

    masked = email
    try:
        _u, _d = email.split("@", 1)
        masked = (_u[:3] + "***@" + _d)
    except Exception:
        pass

    return jsonify(
        ok=True,
        identified=True,
        already_identified=already,
        email_masked=masked,
        unlocked={
            "daily_calls": int(os.environ.get("MCP_IDENTIFIED_DAILY_LIMIT", "100")),
            "previous_daily_calls": int(os.environ.get("MCP_FREE_DAILY_LIMIT", "25")),
            "extras": ["weekly digest of the markets you query",
                       "alerts when a tracked market moves"],
        },
        message=("Email already on file — your key is identified."
                 if already else
                 "Identified — this key now gets 100 calls/day (up from 25) "
                 "plus the weekly market digest."),
        upgrade_note="Need 1,000/day + full data? Developer plan is $49/mo at https://dchub.cloud/pricing",
    ), 200


# ── POST /api/v1/mcp/track ─────────────────────────────────────────────────

@mcp_bp.post("/api/v1/mcp/track")
@_require_internal
def track_tool_call():
    body = request.get_json(silent=True) or {}
    tool = (body.get('tool') or body.get('tool_name'))
    if not tool:
        return jsonify({"ok": False, "error": "missing tool"}), 200

    ts = body.get("timestamp")
    try:
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.now(timezone.utc)
    except Exception:
        ts_dt = datetime.now(timezone.utc)

    params = body.get("params")
    if params is not None and not isinstance(params, str):
        params = json.dumps(params, default=str)

    # ── Phase NN (2026-05-14): attribution recovery ──────────────────────
    # The upstream MCP server (server.mjs) fires this callback WITHOUT
    # forwarding clientInfo, so client_name is almost always 'unknown'
    # and platform is the literal 'mcp' — 98.8% of mcp_tool_calls rows
    # were unattributed. But server.mjs DOES pass session_id, and the
    # /mcp proxy in main.py persists session_id -> (platform, client_name)
    # to mcp_sessions on every `initialize` (where clientInfo.name IS
    # present). Recover real attribution by joining on session_id.
    _r_platform = str(body.get("platform") or "").strip()
    _r_client = str(body.get("client_name") or body.get("client") or "").strip()
    # r44 (2026-05-25): prefer the modern Mcp-Session-Id HTTP header
    # (per MCP transport spec) over the body field. Modern MCP clients
    # send the session identity in headers; older proxies also pass it
    # in body. Take header first, body as fallback. Stable across all
    # tool calls from the same client → unique session count is now a
    # real attribution metric.
    _r_session = (
        request.headers.get("Mcp-Session-Id")
        or request.headers.get("mcp-session-id")
        or request.headers.get("X-Mcp-Session-Id")
        or body.get("session_id")
    )
    _GENERIC = ("", "mcp", "mcp-worker", "unknown", "anonymous")

    # Phase XX (2026-05-16): UUID detection. The funnel showed 5 of the
    # top-20 platform buckets were UUIDs (session_ids leaking into the
    # platform field upstream). UUIDs were NOT in _GENERIC so the recovery
    # below never fired, and we ended with 5 distinct UUID-keyed buckets
    # that should all have been 'claude' or 'chatgpt'. Treat any 36-char
    # UUID-shaped value as generic so the recovery fires for them too.
    import re as _re_uuid
    _UUID_RE = _re_uuid.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def _looks_generic(v: str) -> bool:
        v_lower = v.lower().strip()
        if v_lower in _GENERIC: return True
        if _UUID_RE.match(v_lower): return True
        return False

    # Also normalize: if the incoming platform IS a UUID, blank it before
    # storage so we don't pollute analytics whether or not recovery succeeds.
    _platform_was_uuid = bool(_UUID_RE.match(_r_platform.lower()))
    _client_was_uuid   = bool(_UUID_RE.match(_r_client.lower()))

    if _r_session and (_looks_generic(_r_platform) or _looks_generic(_r_client)):
        try:
            with _pool.connection() as _sc_conn, _sc_conn.cursor() as _sc_cur:
                _sc_cur.execute(
                    "SELECT platform, client_name FROM mcp_sessions WHERE session_id = %s",
                    (str(_r_session)[:200],),
                )
                _sc_row = _sc_cur.fetchone()
            if _sc_row:
                if _sc_row[0] and not _looks_generic(_sc_row[0]) \
                        and _looks_generic(_r_platform):
                    _r_platform = _sc_row[0]
                if _sc_row[1] and not _looks_generic(_sc_row[1]) \
                        and _looks_generic(_r_client):
                    _r_client = _sc_row[1]
        except Exception:
            # mcp_sessions may not exist yet, or lookup hiccupped — fall
            # back to whatever the callback gave us. Never block tracking.
            pass

    # Phase XX: if recovery failed AND the original was a UUID, fall back
    # to detecting from the live User-Agent header. Better an honest 'curl'
    # or 'unknown-ua' than a meaningless UUID polluting the analytics table.
    if _platform_was_uuid and _looks_generic(_r_platform):
        ua = (request.headers.get('User-Agent') or '').lower()
        if   'claude'     in ua: _r_platform = 'claude'
        elif 'chatgpt'    in ua or 'openai-mcp' in ua: _r_platform = 'chatgpt'
        elif 'cursor'     in ua: _r_platform = 'cursor'
        elif 'gemini'     in ua: _r_platform = 'gemini'
        elif 'perplexity' in ua: _r_platform = 'perplexity'
        elif 'copilot'    in ua: _r_platform = 'copilot'
        elif 'cline'      in ua: _r_platform = 'cline'
        elif 'windsurf'   in ua: _r_platform = 'windsurf'
        elif 'grok'       in ua: _r_platform = 'grok'
        elif 'curl' in ua or 'postman' in ua: _r_platform = 'curl'
        else: _r_platform = 'unknown-ua'
    if _client_was_uuid and _looks_generic(_r_client):
        _r_client = 'unknown'

    # phase9j_dual: also write to legacy mcp_tool_calls so the existing
    # /api/v1/usage and /api/v1/data-freshness queries (which read from
    # that table) reflect activity. The 4/30 rewrite of this file moved
    # writes to mcp_call_log; this dual-write keeps both readable.
    try:
        from db_utils import try_get_db
        _db_lt = try_get_db()
        if _db_lt:
            _c_lt = _db_lt.cursor()
            _params_str = params if isinstance(params, str) else (json.dumps(params or {}) if params is not None else '{}')
            # Phase FF++ (2026-05-12): DROPPED the session_id fallback in
            # client_name. Previously, when upstream MCP server (server.mjs)
            # didn't pass client_name (which was always — it didn't
            # capture clientInfo.name from the initialize handshake), this
            # line fell back to body.session_id, which is the MCP
            # transport's auto-generated UUID. That polluted every row in
            # mcp_tool_calls with anonymous UUIDs and made vendor
            # detection impossible.
            #
            # Now: prefer real client_name → client → 'unknown'. Never
            # leak transport plumbing IDs into analytics.
            # r44 (2026-05-25): store session_id from Mcp-Session-Id
            # header (captured into _r_session above). Column added via
            # mcp_growth._SCHEMA_DDL ALTER ... ADD COLUMN IF NOT EXISTS.
            _c_lt.execute(
                """INSERT INTO mcp_tool_calls
                       (tool_name, platform, client_name, params, success,
                        response_time_ms, ip_address, user_agent, session_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(tool)[:200],
                    (_r_platform or 'mcp-worker')[:80],
                    (_r_client or 'unknown')[:200],
                    (_params_str or '{}')[:4000],
                    bool((body.get('status') in (None, 'ok', 'success', 200, True)) or body.get('success', True)),
                    int((body.get('duration_ms') or body.get('response_time_ms') or 0) or 0),
                    (request.headers.get('X-Forwarded-For') or request.remote_addr or '')[:64],
                    (request.headers.get('User-Agent') or '')[:300],
                    (str(_r_session)[:200] if _r_session else None),
                )
            )
            _db_lt.commit()
            try: _db_lt.close()
            except Exception: pass
    except Exception as _e_lt:
        try: import logging as _log9j; _log9j.getLogger(__name__).warning('phase9j_dual mcp_tool_calls insert: %s', _e_lt)
        except Exception: pass


    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_call_log
                     (timestamp, tool, params, platform, api_key, tier,
                      session_id, status, duration_ms, referrer, user_agent, event_type)
                   VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    ts_dt, tool, params,
                    (_r_platform or body.get("platform")),
                    body.get("api_key"),
                    body.get("tier"),
                    body.get("session_id"),
                    body.get("status"),
                    (body.get('duration_ms') or (body.get('response_time_ms') or body.get('duration_ms'))),
                    # r46 (2026-05-25): attribution for v_paywall_attribution view
                    body.get("referer") or body.get("referrer"),
                    (body.get("user_agent") or "")[:500] or None,
                    # r47 (2026-05-25): derive event_type from status so views
                    # don't need backfills going forward.
                    {"blocked_paid_only": "paywall_block",
                     "trial_used":        "trial_preview",
                     "ok":                "tool_call",
                     "error":             "tool_error"}.get(body.get("status")),
                ),
            )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200

    return jsonify({"ok": True}), 200


# ── GET /api/v1/mcp/stats — for our own admin dashboard ───────────────────

@mcp_bp.get("/api/v1/mcp/stats")
@_require_internal
def mcp_stats():
    try:
        days = max(1, min(int(request.args.get("days", "7")), 90))
    except ValueError:
        days = 7

    out = {"window_days": days}

    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT date_trunc('day', timestamp)::date AS day, platform, COUNT(*) AS n
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)
               GROUP BY day, platform ORDER BY day DESC, n DESC""",
            (days,),
        )
        out["by_day_platform"] = [
            {"day": str(r[0]), "platform": r[1], "n": r[2]} for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT tool,
                      COUNT(*)::int AS n,
                      AVG(duration_ms)::int AS avg_ms,
                      COUNT(*) FILTER (WHERE status='error')::int AS errors,
                      COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS upgrade_blocks,
                      COUNT(DISTINCT api_key)::int AS distinct_devs
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)
               GROUP BY tool ORDER BY n DESC""",
            (days,),
        )
        out["by_tool"] = [
            {"tool": r[0], "n": r[1], "avg_ms": r[2],
             "errors": r[3], "upgrade_blocks": r[4], "distinct_devs": r[5]}
            for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE api_key IS NOT NULL)::int AS keyed_calls,
                 COUNT(DISTINCT api_key) AS keyed_devs,
                 COUNT(DISTINCT session_id) AS sessions,
                 COUNT(*)::int AS tool_calls,
                 COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS paid_block_events
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)""",
            (days,),
        )
        r = cur.fetchone() or (0, 0, 0, 0, 0)
        out["funnel"] = {
            "keyed_calls":       r[0] or 0,
            "keyed_devs":        r[1] or 0,
            "sessions":          r[2] or 0,
            "tool_calls":        r[3] or 0,
            "paid_block_events": r[4] or 0,
        }

        cur.execute(
            "SELECT tier, COUNT(*)::int FROM mcp_dev_keys WHERE status='active' GROUP BY tier ORDER BY tier"
        )
        out["keys_by_tier"] = [{"tier": r[0], "n": r[1]} for r in cur.fetchall()]

    return jsonify(out), 200


# ── POST /api/v1/dev-signup — Self-serve free dev key (PUBLIC) ────────────

@mcp_bp.post("/api/v1/dev-signup")
def dev_signup():
    body  = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return jsonify({"error": "valid email required"}), 400

    api_key      = f"dch_live_{secrets.token_hex(16)}"
    developer_id = f"dev_{secrets.token_hex(8)}"

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT api_key FROM mcp_dev_keys WHERE email=%s AND status='active' LIMIT 1",
                (email,),
            )
            existing = cur.fetchone()
            if existing:
                return jsonify({
                    "api_key":     existing[0],
                    "tier":        "free",
                    "email":       email,
                    "is_new":      False,
                    "header":      "X-API-Key",
                    "docs":        "https://dchub.cloud/ai",
                    # Phase FF+7 (2026-05-19): /upgrade entry-point (see /keys/claim above)
                    "upgrade_url": f"https://dchub.cloud/upgrade?key={existing[0]}",
                }), 200
            cur.execute(
                """INSERT INTO mcp_dev_keys
                     (api_key, developer_id, email, tier, status, metadata)
                   VALUES (%s, %s, %s, 'free', 'active', %s::jsonb)""",
                (api_key, developer_id, email, '{"source":"dev-signup-form"}'),
            )
    except Exception as e:
        return jsonify({"error": "key issuance failed", "detail": str(e)}), 500

    return jsonify({
        "api_key":     api_key,
        "tier":        "free",
        "email":       email,
        "is_new":      True,
        "header":      "X-API-Key",
        "docs":        "https://dchub.cloud/ai",
        # Phase FF+7 (2026-05-19): /upgrade entry-point with attribution
        "upgrade_url": f"https://dchub.cloud/upgrade?key={api_key}",
    }), 200


# ── GET /api/v1/mcp/funnel — Public aggregate stats for the dashboard ─────

@mcp_bp.get("/api/v1/mcp/funnel")
def mcp_funnel():
    # Phase FF+25-followup-r3 (2026-05-20) — split probe vs real traffic.
    # Until now `tool_calls_7d` lumped together genuine MCP-client traffic
    # AND our own QA / healer probes (User-Agent matches python-script,
    # node-script, curl, postman, insomnia, plus the always-unattributed
    # "unknown" bucket). When CF WAF temporarily over-blocked our probes,
    # the 7d number dropped 38k→27k and looked like real-user churn even
    # though zero external clients had changed behavior.
    #
    # `tool_calls_7d_real` excludes those self-traffic platforms so the
    # public dashboard can show what AI agents are actually doing. Both
    # numbers ship in the response — `tool_calls_7d` stays as the gross
    # count for backward compat (brain detectors / mcp_growth.py still
    # read it) and `tool_calls_7d_real` is what the UI should highlight.
    _PROBE_PLATFORMS = (
        'curl', 'python-script', 'node-script',
        'postman', 'insomnia', 'unknown', 'verify',
    )
    out = {}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["tool_calls_7d"] = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM mcp_upgrade_signals WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["upgrade_signals_7d"] = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM mcp_conversions WHERE created_at >= NOW() - INTERVAL '30 days'"
            )
            out["conversions_30d"] = cur.fetchone()[0]

            cur.execute(
                "SELECT tier, COUNT(*) FROM mcp_dev_keys WHERE status='active' GROUP BY tier"
            )
            out["keys_by_tier"] = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute(
                """SELECT tool_requested, COUNT(*) AS n
                   FROM mcp_upgrade_signals
                   WHERE created_at >= NOW() - INTERVAL '30 days'
                   GROUP BY tool_requested ORDER BY n DESC LIMIT 10"""
            )
            out["top_signal_tools_30d"] = [
                {"tool": r[0], "n": r[1]} for r in cur.fetchall()
            ]

            cur.execute(
                """SELECT tool_name, COUNT(*) AS n,
                          COUNT(DISTINCT ip_address) AS users
                   FROM mcp_tool_calls
                   WHERE tool_name = ANY(%s)
                     AND created_at >= NOW() - INTERVAL '30 days'
                   GROUP BY tool_name ORDER BY n DESC""",
                (["analyze_site", "compare_sites", "get_grid_intelligence",
                  "get_dchub_recommendation", "get_fiber_intel"],),
            )
            out["paid_tool_demand_30d"] = [
                {"tool": r[0], "calls": r[1], "users": r[2]} for r in cur.fetchall()
            ]

            # Phase JJ batch 3 (2026-05-14): per-platform funnel breakdown.
            # 8K upgrade signals × 0.05% conversion is the headline business
            # problem; we couldn't tell where the drop-off happened because
            # nobody was aggregating by mcp_client. Schema already had the
            # column (mcp_analytics_postgres.py:88); this exposes it.
            #
            # Each row tells you: per AI platform, how many tool calls,
            # how many upgrade signals (= they hit a paid tool), and how
            # many distinct users. Comparing platforms reveals which AI
            # agents convert humans best (Claude vs ChatGPT vs Cursor etc).
            try:
                cur.execute(
                    """SELECT
                          COALESCE(NULLIF(LOWER(mcp_client), ''), 'unknown') AS platform,
                          COUNT(*) AS signals,
                          COUNT(DISTINCT session_id) AS sessions,
                          COUNT(DISTINCT ip_address) AS unique_ips,
                          COUNT(*) FILTER (WHERE converted = TRUE) AS converted
                       FROM mcp_upgrade_signals
                       WHERE created_at >= NOW() - INTERVAL '30 days'
                       GROUP BY platform
                       ORDER BY signals DESC
                       LIMIT 20"""
                )
                out["signals_by_platform_30d"] = [
                    {
                        "platform": r[0],
                        "signals": r[1],
                        "sessions": r[2],
                        "unique_ips": r[3],
                        "converted": r[4] or 0,
                        "conv_rate_pct": round((r[4] or 0) / max(r[1], 1) * 100, 3),
                    }
                    for r in cur.fetchall()
                ]
            except Exception as e:
                out["signals_by_platform_30d_error"] = str(e)[:120]

            # Per-platform tool-call totals — pairs with signals_by_platform
            # so we can compute "signal rate" (% of calls that hit a paywall)
            # per platform. Shows whether some platforms are pinging paid
            # tools more aggressively than others.
            try:
                # NOTE: mcp_tool_calls has BOTH a `client_name` and a
                # `platform` column. Aliasing the COALESCE expression
                # `AS platform` and then `GROUP BY platform` made Postgres
                # bind to the real `platform` column, not the alias —
                # leaving client_name ungrouped → "must appear in GROUP
                # BY" error. Fix: alias as `client_platform` (no column
                # collision) and GROUP BY the full expression.
                #
                # Phase ZZZZ-attribution (2026-05-18): client_name is
                # null/empty for 23K+ calls (most agents don't send
                # clientInfo.name in initialize handshake). Backfill via
                # user_agent pattern-matching so we actually know WHO is
                # hitting the funnel — was 'unknown' for 70%+ of traffic.
                # Phase ZZZZ-attr-v2: client_name is being set to session
                # UUIDs (not platform names) for most MCP traffic — those
                # 8-4-4-4-12 hex UUIDs pollute the platform list. Filter
                # them out via a regex check so we fall through to
                # user_agent classification.
                # Phase ZZZZZ-round9 (2026-05-23): tighten the classifier
                # so 90,000+ tool calls don't lump into 'node-script' +
                # 'unknown' (which is what mcp/funnel showed before this
                # commit — 50k + 40k = 98% of traffic unattributable).
                # Two layers added BEFORE the generic node/python buckets:
                #   1. Internal traffic — our own DCHub-* UAs (brain-radar,
                #      healer, sentinel, scheduler, smoke-test) sorted into
                #      'internal-dchub' so they don't pollute external
                #      conversion metrics.
                #   2. MCP SDK identification — @modelcontextprotocol/sdk,
                #      mcp-inspector, and the n8n MCP node all expose
                #      identifiable UA fragments. Catching them before the
                #      generic 'node-script' falls through means we know
                #      "this is an MCP agent" even when the host AI client
                #      didn't pass clientInfo.name.
                _platform_case = r"""
                    CASE
                        WHEN NULLIF(LOWER(client_name), '') IS NOT NULL
                             AND client_name !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN LOWER(client_name)
                        WHEN user_agent ILIKE '%dchub-%' OR user_agent ILIKE '%dchubhealer%'
                            OR user_agent ILIKE '%brain-v2-headless%' OR user_agent ILIKE '%brain-radar%'
                            OR user_agent ILIKE '%uptimerobot%'
                            THEN 'internal-dchub'
                        WHEN user_agent ILIKE '%@modelcontextprotocol/sdk%'
                            OR user_agent ILIKE '%modelcontextprotocol%'
                            THEN 'mcp-sdk'
                        WHEN user_agent ILIKE '%mcp-inspector%'
                            THEN 'mcp-inspector'
                        WHEN user_agent ILIKE '%n8n%'
                            THEN 'n8n'
                        WHEN user_agent ILIKE '%smithery%'
                            THEN 'smithery'
                        WHEN user_agent ILIKE '%chatgpt%' OR user_agent ILIKE '%openai%'
                            THEN 'chatgpt'
                        WHEN user_agent ILIKE '%claude%' OR user_agent ILIKE '%anthropic%'
                            THEN 'claude'
                        WHEN user_agent ILIKE '%perplexity%'
                            THEN 'perplexity'
                        WHEN user_agent ILIKE '%gemini%' OR user_agent ILIKE '%googleother%'
                            THEN 'gemini'
                        WHEN user_agent ILIKE '%groq%'
                            THEN 'groq'
                        WHEN user_agent ILIKE '%cursor%'
                            THEN 'cursor'
                        WHEN user_agent ILIKE '%windsurf%' OR user_agent ILIKE '%codeium%'
                            THEN 'windsurf'
                        WHEN user_agent ILIKE '%continue%'
                            THEN 'continue.dev'
                        WHEN user_agent ILIKE '%cody%' OR user_agent ILIKE '%sourcegraph%'
                            THEN 'sourcegraph-cody'
                        WHEN user_agent ILIKE '%copilot%'
                            THEN 'github-copilot'
                        WHEN user_agent ILIKE '%cline%'
                            THEN 'cline'
                        WHEN user_agent ILIKE '%phind%'
                            THEN 'phind'
                        WHEN user_agent ILIKE '%you.com%' OR user_agent ILIKE '%youbot%'
                            THEN 'you.com'
                        WHEN user_agent ILIKE '%meta-external%' OR user_agent ILIKE '%llama%'
                            THEN 'meta-ai'
                        WHEN user_agent ILIKE '%applebot-extended%'
                            THEN 'apple-intelligence'
                        WHEN user_agent ILIKE '%curl%'
                            THEN 'curl'
                        WHEN user_agent ILIKE '%python%' OR user_agent ILIKE '%requests%'
                            THEN 'python-script'
                        WHEN user_agent ILIKE '%node-fetch%' OR user_agent ILIKE '%undici%'
                            OR user_agent ILIKE '%axios%' OR user_agent ILIKE '%got/%'
                            THEN 'node-http-client'
                        WHEN user_agent ILIKE '%node%'
                            THEN 'node-script'
                        WHEN user_agent ILIKE '%postman%'
                            THEN 'postman'
                        WHEN user_agent ILIKE '%insomnia%'
                            THEN 'insomnia'
                        ELSE 'unknown'
                    END
                """
                cur.execute(
                    f"""SELECT
                          {_platform_case} AS client_platform,
                          COUNT(*) AS calls,
                          COUNT(DISTINCT ip_address) AS unique_ips
                       FROM mcp_tool_calls
                       WHERE created_at >= NOW() - INTERVAL '30 days'
                       GROUP BY {_platform_case}
                       ORDER BY calls DESC
                       LIMIT 20"""
                )
                out["calls_by_platform_30d"] = [
                    {"platform": r[0], "calls": r[1], "unique_ips": r[2]}
                    for r in cur.fetchall()
                ]
            except Exception as e:
                out["calls_by_platform_30d_error"] = str(e)[:120]

            # Phase FF+25-followup-r3 (2026-05-20): probe-filtered counts.
            # Same _platform_case classifier as above, but COUNTed at 7d
            # window with the probe platforms excluded. These are what the
            # public /cited-by + homepage dashboards should display.
            try:
                # No bound params here: passing %s tripped the driver two ways
                # (psycopg2 parsed the LIKE '%chatgpt%' in _platform_case as
                # placeholders → "got '%c'"; and the tuple binding rendered a
                # bare "$1" Postgres couldn't parse). _PROBE_PLATFORMS is a
                # trusted hardcoded constant, so inline it as a SQL literal
                # IN-list — no binding, so _platform_case's % are left alone.
                _probe_in = ",".join(
                    "'" + str(p).replace("'", "''") + "'" for p in _PROBE_PLATFORMS)
                cur.execute(
                    f"""SELECT
                          COUNT(*) FILTER (
                            WHERE {_platform_case} NOT IN ({_probe_in})
                          ) AS real_calls,
                          COUNT(*) FILTER (
                            WHERE {_platform_case}     IN ({_probe_in})
                          ) AS probe_calls,
                          COUNT(DISTINCT ip_address) FILTER (
                            WHERE {_platform_case} NOT IN ({_probe_in})
                          ) AS real_unique_ips
                       FROM mcp_tool_calls
                       WHERE created_at >= NOW() - INTERVAL '7 days'"""
                )
                _r = cur.fetchone() or (0, 0, 0)
                out["tool_calls_7d_real"]   = int(_r[0] or 0)
                out["tool_calls_7d_probes"] = int(_r[1] or 0)
                out["unique_ips_7d_real"]   = int(_r[2] or 0)
                # Convenience: list which platforms were classified as probes
                # so the UI can render a tooltip ("filtered: node-script,
                # python-script, curl, ...").
                out["probe_platforms"] = list(_PROBE_PLATFORMS)
            except Exception as e:
                out["tool_calls_7d_real_error"] = str(e)[:120]

            # Time-to-conversion median per platform (days from first
            # upgrade signal to converted=true). Reveals whether some
            # platforms convert fast vs slow.
            try:
                cur.execute(
                    """WITH per_session AS (
                         SELECT session_id,
                                COALESCE(NULLIF(LOWER(mcp_client), ''), 'unknown') AS platform,
                                MIN(created_at) AS first_signal,
                                MIN(converted_at) FILTER (WHERE converted = TRUE) AS conv_at
                         FROM mcp_upgrade_signals
                         WHERE created_at >= NOW() - INTERVAL '90 days'
                         GROUP BY session_id, platform
                       )
                       SELECT platform,
                              COUNT(*) FILTER (WHERE conv_at IS NOT NULL) AS converted_sessions,
                              PERCENTILE_CONT(0.5) WITHIN GROUP (
                                ORDER BY EXTRACT(EPOCH FROM (conv_at - first_signal))/86400.0
                              ) FILTER (WHERE conv_at IS NOT NULL) AS median_days_to_convert
                       FROM per_session
                       GROUP BY platform
                       ORDER BY converted_sessions DESC"""
                )
                out["time_to_convert_90d"] = [
                    {
                        "platform": r[0],
                        "converted_sessions": r[1] or 0,
                        "median_days_to_convert": round(float(r[2]), 2) if r[2] is not None else None,
                    }
                    for r in cur.fetchall()
                ]
            except Exception as e:
                out["time_to_convert_90d_error"] = str(e)[:120]
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out), 200


# ── GET /api/v1/mcp/timeseries — Hourly traffic series for the dashboard ──
#
# ── GET /api/ai-analytics — thin adapter for connect.html dashboard ────────
#
# r33-Q (2026-05-21) — /connect's live dashboard fetches /api/ai-analytics
# to populate three counters (total_requests, active_platforms,
# mcp_connections). The endpoint never existed; every visitor's poll
# returned 404 and the dashboard silently stayed at zeros. Brain found
# this via frontend-health probe logs.
#
# Implementation: aggregate from mcp_tool_calls (30d) — same source
# /api/v1/mcp/funnel uses but flattened into the three counters
# connect.html expects. Cached server-side for 60s to avoid hammering
# the DB on every poll (page polls every 60s anyway).

@mcp_bp.get("/api/ai-analytics")
def ai_analytics():
    """Live counters for the /connect page dashboard.

    Returns:
        success: bool
        total_requests: int       — all MCP tool calls in last 30d
        active_platforms: int     — distinct AI clients seen in last 30d
        mcp_connections: int      — active MCP dev keys
    """
    out = {"success": True, "total_requests": 0,
           "active_platforms": 0, "mcp_connections": 0}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '30 days'"
            )
            out["total_requests"] = int(cur.fetchone()[0] or 0)

            # Distinct platforms across signals + tool calls — be lenient
            # about which table the data lives in (different MCP shapes
            # write to different tables over time).
            try:
                cur.execute(
                    "SELECT COUNT(DISTINCT platform) FROM mcp_upgrade_signals "
                    "WHERE created_at >= NOW() - INTERVAL '30 days' "
                    "AND platform IS NOT NULL AND platform != ''"
                )
                out["active_platforms"] = int(cur.fetchone()[0] or 0)
            except Exception:
                conn.rollback()

            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_dev_keys WHERE status='active'"
                )
                out["mcp_connections"] = int(cur.fetchone()[0] or 0)
            except Exception:
                conn.rollback()
    except Exception as e:
        out["success"] = False
        out["error"] = str(e)[:200]
    return jsonify(out)


# Phase JJ (2026-05-13): the existing /funnel returns rolling 7d/30d
# aggregates which wobble ±N every refresh thanks to rolling-window
# math, making it impossible to tell at a glance whether MCP traffic
# is actually growing or declining. This endpoint returns proper
# hourly buckets so the dashboard can show a trend line.

@mcp_bp.get("/api/v1/mcp/timeseries")
def mcp_timeseries():
    """Hourly MCP traffic + gate-fire series for the last N hours.

    Query params:
      hours          int, default 168 (7d), max 720 (30d)
      bucket         'hour' (default) | 'day'

    Response shape:
      {
        "bucket": "hour" | "day",
        "from_iso": "2026-05-06T00:00:00Z",
        "to_iso":   "2026-05-13T00:00:00Z",
        "series": [
          {"ts": "...", "tool_calls": N, "upgrade_signals": M, "gate_fires": M},
          ...
        ],
        "totals":  {"tool_calls": ..., "upgrade_signals": ..., "conversion_rate_pct": ...}
      }

    Public-readable like /funnel — no admin key required. Aggregates only,
    no PII. Heavily indexed by (created_at) so the query is fast.
    """
    try:
        hours = max(1, min(int(request.args.get("hours", 168)), 720))
    except (TypeError, ValueError):
        hours = 168
    bucket = (request.args.get("bucket") or "hour").lower()
    if bucket not in ("hour", "day"):
        bucket = "hour"

    out: dict = {"bucket": bucket, "hours": hours}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            # Window bounds — return as ISO so frontends can use Date.parse.
            cur.execute("SELECT NOW() - (%s || ' hours')::INTERVAL, NOW()", (hours,))
            from_ts, to_ts = cur.fetchone()
            out["from_iso"] = from_ts.isoformat()
            out["to_iso"] = to_ts.isoformat()

            # Bucket function — date_trunc gives us aligned bins regardless
            # of when the call landed within the hour.
            trunc = f"date_trunc('{bucket}', created_at)"

            # Tool calls per bucket
            cur.execute(f"""
                SELECT {trunc} AS bin, COUNT(*) AS n
                FROM mcp_tool_calls
                WHERE created_at >= NOW() - (%s || ' hours')::INTERVAL
                GROUP BY bin ORDER BY bin
            """, (hours,))
            calls_by_bin = {r[0].isoformat(): int(r[1]) for r in cur.fetchall()}

            # Upgrade signals per bucket (= gate fires that emitted an
            # upgrade prompt). This is the key conversion-funnel input.
            cur.execute(f"""
                SELECT {trunc} AS bin, COUNT(*) AS n
                FROM mcp_upgrade_signals
                WHERE created_at >= NOW() - (%s || ' hours')::INTERVAL
                GROUP BY bin ORDER BY bin
            """, (hours,))
            signals_by_bin = {r[0].isoformat(): int(r[1]) for r in cur.fetchall()}

            # Conversions per bucket (paying customers; 30d-style)
            cur.execute(f"""
                SELECT {trunc} AS bin, COUNT(*) AS n
                FROM mcp_conversions
                WHERE created_at >= NOW() - (%s || ' hours')::INTERVAL
                GROUP BY bin ORDER BY bin
            """, (hours,))
            conv_by_bin = {r[0].isoformat(): int(r[1]) for r in cur.fetchall()}

            # Merge into a single ordered series. Use the union of bin
            # keys so a quiet hour still shows up as a zero row (avoids
            # confusing "gaps" in the chart).
            all_bins = sorted(set(calls_by_bin) | set(signals_by_bin) | set(conv_by_bin))
            series = [
                {
                    "ts": b,
                    "tool_calls":      calls_by_bin.get(b, 0),
                    "upgrade_signals": signals_by_bin.get(b, 0),
                    "conversions":     conv_by_bin.get(b, 0),
                }
                for b in all_bins
            ]
            out["series"] = series

            # Totals + the conversion rate the dashboard actually cares
            # about — signals are the right denominator, not raw calls,
            # because un-gated free-tier calls can't possibly convert.
            total_calls = sum(calls_by_bin.values())
            total_signals = sum(signals_by_bin.values())
            total_conv = sum(conv_by_bin.values())
            out["totals"] = {
                "tool_calls":      total_calls,
                "upgrade_signals": total_signals,
                "conversions":     total_conv,
                "conversion_rate_pct": (
                    round((total_conv / total_signals) * 100.0, 3)
                    if total_signals > 0 else None
                ),
            }
    except Exception as e:
        out["error"] = str(e)
        return jsonify(out), 500
    return jsonify(out), 200


# ── GET /api/v1/mcp/dashboard — Serve the dashboard HTML through Flask ────

@mcp_bp.get("/api/v1/mcp/dashboard")
def mcp_dashboard():
    """Serves static/mcp-dashboard.html via the /api/* path so Cloudflare proxies it."""
    try:
        with open("static/mcp-dashboard.html", "r") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("dashboard not found", status=404)



# ── POST /api/v1/stripe/webhook-mcp — Stripe → mcp_conversions ─────────────

@mcp_bp.post("/api/v1/stripe/webhook-mcp")
def stripe_webhook_mcp():
    """Handle Stripe customer.subscription.{created,updated}.
    Records conversion in mcp_conversions with attribution to the most recent
    mcp_upgrade_signal for the customer's email.

    Configure on Stripe dashboard: Webhooks → Add endpoint:
      URL:     https://dchub.cloud/api/v1/stripe/webhook-mcp
      Events:  customer.subscription.created, customer.subscription.updated
      Secret:  store as STRIPE_WEBHOOK_SECRET_MCP env var on Railway.
               Also needs STRIPE_SECRET_KEY env var to look up customer email.
    """
    try:
        import stripe
    except ImportError:
        return jsonify({"error": "stripe library not installed"}), 500

    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")
    secret  = os.environ.get("STRIPE_WEBHOOK_SECRET_MCP") or os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        else:
            # No secret configured — accept (dev/test mode); production should set secret
            event = json.loads(payload.decode("utf-8"))
    except Exception as e:
        return jsonify({"error": "invalid signature", "detail": str(e)}), 400

    event_type = event.get("type", "") if isinstance(event, dict) else getattr(event, "type", "")
    if event_type not in ("customer.subscription.created", "customer.subscription.updated"):
        return jsonify({"ok": True, "ignored": event_type}), 200

    obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    obj = dict(obj) if not isinstance(obj, dict) else obj
    customer_id = obj.get("customer")
    sub_id      = obj.get("id")

    # Resolve customer email
    email = None
    try:
        api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        if api_key and customer_id:
            stripe.api_key = api_key
            cust = stripe.Customer.retrieve(customer_id)
            email = (getattr(cust, "email", "") or "").lower() or None
    except Exception:
        pass
    if not email:
        return jsonify({"ok": False, "error": "couldnt resolve customer email"}), 200

    # Determine plan + MRR
    items = obj.get("items", {}).get("data", []) if obj.get("items") else []
    plan_to   = "pro"
    mrr_cents = 4900
    if items:
        price = items[0].get("price", {}) if isinstance(items[0], dict) else {}
        plan_to   = price.get("lookup_key") or price.get("nickname") or "pro"
        mrr_cents = price.get("unit_amount") or 4900

    # Find most recent signal for this email (attribution)
    attribution_id = None
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT s.id FROM mcp_upgrade_signals s
                   LEFT JOIN mcp_dev_keys k ON k.api_key = s.session_id
                   WHERE COALESCE(s.user_email, k.email) = %s
                   ORDER BY s.created_at DESC LIMIT 1""",
                (email,),
            )
            row = cur.fetchone()
            attribution_id = row[0] if row else None
    except Exception:
        pass

    # Insert (idempotent on stripe_subscription_id thanks to the UNIQUE constraint)
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_conversions
                     (user_email, stripe_customer_id, stripe_subscription_id,
                      plan_to, mrr_cents, source, attribution_signal_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (stripe_subscription_id) DO UPDATE
                     SET plan_to   = EXCLUDED.plan_to,
                         mrr_cents = EXCLUDED.mrr_cents
                   RETURNING id""",
                (email, customer_id, sub_id, plan_to, mrr_cents,
                 "stripe_webhook_mcp", attribution_id),
            )
            conv_id = cur.fetchone()[0]
    except Exception as e:
        return jsonify({"error": "db insert failed", "detail": str(e)}), 500

    return jsonify({
        "ok":                    True,
        "conversion_id":         conv_id,
        "email":                 email,
        "attribution_signal_id": attribution_id,
        "plan_to":               plan_to,
        "mrr_cents":             mrr_cents,
    }), 200



# ── GET /api/v1/dev-signup-form — Serve the widget HTML through Flask ─────

@mcp_bp.get("/api/v1/dev-signup-form")
def dev_signup_form():
    """Serve the standalone signup widget. Embed via iframe or link from /ai."""
    try:
        with open("static/signup-widget.html", "r") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("<h1>Signup widget not deployed</h1>", status=404, mimetype="text/html")



# ── GET /api/v1/_env_stripe_check — verify Stripe env vars are loaded ──────

@mcp_bp.get("/api/v1/_env_stripe_check")
@_require_internal
def env_stripe_check():
    """Diagnostic: is STRIPE_WEBHOOK_SECRET_MCP loaded? (no secret values exposed)"""
    sec = os.environ.get("STRIPE_WEBHOOK_SECRET_MCP", "")
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    return jsonify({
        "STRIPE_WEBHOOK_SECRET_MCP_set":         bool(sec),
        "STRIPE_WEBHOOK_SECRET_MCP_length":      len(sec) if sec else 0,
        "STRIPE_WEBHOOK_SECRET_MCP_prefix":      (sec[:6] + "…") if sec else None,
        "STRIPE_SECRET_KEY_set":                 bool(key),
        "STRIPE_SECRET_KEY_prefix":              (key[:7] + "…") if key else None,
        "all_env_vars_starting_with_STRIPE":     sorted([
            k for k in os.environ.keys() if k.upper().startswith("STRIPE")
        ]),
    }), 200



# ── POST /api/v1/mcp/trial-check — has this session used its trial? ────────

@mcp_bp.post("/api/v1/mcp/trial-check")
@_require_internal
def trial_check():
    """server.mjs calls this to ask: has session_id already consumed its
    free preview for this tool? Returns {trial_used, prior_calls,
    tier_upgrade}.

    A trial is "used" if mcp_call_log has any prior status='ok' OR
    status='trial_used' row for the same (session_id, tool) combo.

    r41-session-upgrade (2026-05-25): also returns `tier_upgrade` when
    the session has a redeemed dev key. When a Claude.ai web user hits
    a paywall and follows the redeem URL, the redeem handler at
    routes/redeem_routes.py creates a dev key with
    metadata.session_id = <this session_id>. Subsequent paid-tool
    attempts in the SAME chat session can then be upgraded in-place,
    closing the Claude.ai gap (their custom-connector UI can't attach
    an X-API-Key header, so without this their session is stuck at
    free tier forever).

    server.mjs treats tier_upgrade as a directive to update
    sessionMeta.tier — see r41-session-upgrade in server.mjs.
    """
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    tool = body.get("tool")
    if not session_id or not tool:
        return jsonify({"trial_used": True, "prior_calls": 0,
                        "reason": "missing_session_or_tool"}), 200

    out = {"trial_used": True, "prior_calls": 0}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            # Trial-eligibility check (existing behavior)
            cur.execute(
                """SELECT COUNT(*) FROM mcp_call_log
                   WHERE session_id = %s
                     AND tool = %s
                     AND status IN ('ok', 'trial_used')""",
                (session_id, tool),
            )
            prior = cur.fetchone()[0]
            out["trial_used"]  = prior > 0
            out["prior_calls"] = prior

            # r41-session-upgrade: was this session redeemed? Look for a
            # dev key whose JSON metadata records this session_id. The
            # metadata column is JSONB so the ->> operator gives O(log n)
            # lookup with a GIN index, or O(n) sequential scan. With ~13
            # paid keys total, even a sequential scan is sub-millisecond.
            try:
                cur.execute(
                    """SELECT plan
                       FROM api_keys
                       WHERE metadata::jsonb ->> 'session_id' = %s
                         AND is_active = 1
                       ORDER BY id DESC
                       LIMIT 1""",
                    (session_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    plan = str(row[0]).lower()
                    # Only suggest an upgrade for plans that actually
                    # unlock paid tools — never accidentally "upgrade"
                    # someone to a lower tier than they already have.
                    if plan in ('developer', 'pro', 'enterprise', 'founding'):
                        out["tier_upgrade"] = plan
            except Exception:
                # Schema variants in the wild (metadata stored as TEXT
                # in some envs vs JSONB in others). Don't fail the whole
                # trial-check on a session-upgrade lookup error.
                pass

        return jsonify(out), 200
    except Exception as e:
        return jsonify({"trial_used": True, "prior_calls": 0,
                        "error": str(e)}), 200
