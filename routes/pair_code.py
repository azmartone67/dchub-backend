"""Phase DD (2026-05-12) — Agent → Human pair-code conversion flow.

The single biggest conversion lever. Live funnel data shows:
  • 8,107 upgrade signals in 7d
  • 1 conversion in 30d   (= 0.012% rate)

The leak: AI agents are stateless / headless. They CAN'T click a
Stripe Payment Link. They have to verbally relay a URL to a human,
who then has to:
  1. Visit the URL
  2. Complete Stripe checkout
  3. Receive an email with a new API key
  4. Find that key in the email
  5. Copy it back to the agent's config

5-step funnel → most users drop off at step 2.

Pair-code closes the funnel to 2 steps:
  1. Agent hits paywall → response includes
     `pair_code: "DCM-4F7K"` + `redeem_url:
     https://dchub.cloud/redeem/DCM-4F7K`
  2. Human visits the URL → page shows the agent context
     ("Your Claude Desktop just hit a paywall on get_grid_intelligence
     for the Chicago market") + a "Unlock for $49/mo →" button
     that goes straight to Stripe
  3. Stripe completes → webhook redeems the pair_code → THIS APIs key
     gets promoted to Developer tier
  4. Agent's NEXT call unlocks. Zero copy-paste, zero context loss.

The agent polls `GET /api/v1/mcp/pair-code/<code>/status` to know when
the human completes. Once redeemed, the agent's normal API key starts
returning paid data — no key swap needed.

Endpoints
---------
  POST /api/v1/mcp/pair-code/generate        — agent calls with their
                                                api_key; returns a fresh
                                                code (idempotent per
                                                key in a 30-min window)
  GET  /api/v1/mcp/pair-code/<code>/status   — public; agent polls
  GET  /redeem/<code>                        — user landing page (HTML)
  GET  /api/v1/mcp/funnel/diagnostics        — drop-off per stage
                                                (signal → code →
                                                redeem_view → stripe_click
                                                → conversion)

Stripe integration: `/redeem/<code>` builds the Stripe URL with
`?client_reference_id=<code>`. Stripe forwards that to the webhook on
`checkout.session.completed`. The webhook handler (extended in
main.py) calls `redeem_pair_code()` to flip the api_key's tier.
"""
from __future__ import annotations
import os
import sys
import secrets as _secrets
import string
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, request, Response

pair_code_bp = Blueprint("pair_code", __name__)
DATABASE_URL = os.environ.get("DATABASE_URL")
STRIPE_DEVELOPER_LINK = (
    os.environ.get('DCHUB_STRIPE_DEVELOPER_LINK')
    or os.environ.get('DCHUB_STRIPE_PRO_LINK')
    or 'https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c'
)


def _conn():
    if not DATABASE_URL: return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=8)
    except Exception as e:
        print(f"[pair_code] connect failed: {e}", file=sys.stderr)
        return None


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS mcp_pair_codes (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    api_key_hash    TEXT NOT NULL,                       -- never store raw keys
    tool_name       TEXT,                                -- which paid tool triggered
    market          TEXT,                                -- which market the agent queried
    target_tier     TEXT NOT NULL DEFAULT 'developer',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 minutes'),
    redeem_viewed_at TIMESTAMPTZ,                        -- user landed on /redeem
    stripe_clicked_at TIMESTAMPTZ,                       -- user clicked "Upgrade" button
    redeemed_at     TIMESTAMPTZ,
    stripe_session_id TEXT,
    user_agent_at_view TEXT,
    notes           JSONB
);
CREATE INDEX IF NOT EXISTS mcp_pair_codes_code_idx ON mcp_pair_codes(code);
CREATE INDEX IF NOT EXISTS mcp_pair_codes_unredeemed_idx
    ON mcp_pair_codes(api_key_hash, created_at DESC)
    WHERE redeemed_at IS NULL;
"""


def init_schema() -> bool:
    c = _conn()
    if c is None: return False
    try:
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[pair_code] init_schema failed: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


try:
    _SCHEMA_OK = init_schema()
except Exception:
    _SCHEMA_OK = False


def _hash_key(k: str) -> str:
    import hashlib
    return hashlib.sha256((k or "").encode()).hexdigest()[:32]


def _new_code() -> str:
    """Generate a 6-char human-friendly code: `DCM-XXXX` (avoids 0/O/1/I)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "DCM-" + "".join(_secrets.choice(alphabet) for _ in range(4))


# ---------------------------------------------------------------------------
# Core: create / fetch / redeem
# ---------------------------------------------------------------------------

def _detect_agent_from_request() -> str | None:
    """Phase DD+ Play 6: detect the calling AI agent from headers.
       Falls back to None when no agent fingerprint matches. Same
       heuristic as routes/mcp_conversion_plays._capture_agent — keeps
       attribution consistent across both modules.
    """
    try:
        from flask import request as _req
        ua = (_req.headers.get("X-Client-Name")
              or _req.headers.get("User-Agent") or "")
        ua_low = ua.lower()
        for known in ("claude", "cursor", "gpt", "openai", "gemini",
                       "perplexity", "cline", "windsurf", "copilot", "grok"):
            if known in ua_low:
                return known
        return (ua[:60] or None)
    except Exception:
        return None


def get_or_create_code(api_key: str, tool_name: str | None = None,
                       market: str | None = None,
                       referring_agent: str | None = None) -> dict | None:
    """Return an unredeemed code for this api_key (newer than 30min), or
       generate a new one. Idempotent — repeated paywall hits for the
       same key reuse the same code so the human only ever sees one URL.

       Phase DD+ Play 6: also captures referring_agent for affiliate
       attribution. Caller can pass an explicit value; otherwise we
       sniff the User-Agent / X-Client-Name. Stored in mcp_pair_codes
       (column added by mcp_conversion_plays schema migration).
    """
    if not api_key:
        return None
    if referring_agent is None:
        referring_agent = _detect_agent_from_request()
    c = _conn()
    if c is None: return None
    h = _hash_key(api_key)
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT code, expires_at FROM mcp_pair_codes
                WHERE api_key_hash = %s
                  AND redeemed_at IS NULL
                  AND expires_at > NOW()
                ORDER BY created_at DESC LIMIT 1
            """, (h,))
            row = cur.fetchone()
        if row:
            code, exp = row
            return {"code": code,
                    "redeem_url": f"https://dchub.cloud/redeem/{code}",
                    "expires_at": exp.isoformat() if exp else None,
                    "reused": True}
        # Try up to 5 times in case of code collision (extremely unlikely
        # with 32^4 = 1M codes, but defensive). Defensive against the
        # referring_agent column not existing yet (e.g. fresh DB before
        # the Play 6 schema migration ran) — we try with the column,
        # rollback on error, then retry without.
        for attempt in range(5):
            code = _new_code()
            try:
                with c.cursor() as cur:
                    try:
                        cur.execute("""
                            INSERT INTO mcp_pair_codes
                                (code, api_key_hash, tool_name, market,
                                 referring_agent)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (code) DO NOTHING
                            RETURNING expires_at;
                        """, (code, h, tool_name, market, referring_agent))
                    except Exception:
                        c.rollback()
                        # Fall back without the new column
                        cur.execute("""
                            INSERT INTO mcp_pair_codes
                                (code, api_key_hash, tool_name, market)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (code) DO NOTHING
                            RETURNING expires_at;
                        """, (code, h, tool_name, market))
                    inserted = cur.fetchone()
                c.commit()
                if inserted:
                    return {"code": code,
                            "redeem_url": f"https://dchub.cloud/redeem/{code}",
                            "expires_at": inserted[0].isoformat(),
                            "reused": False,
                            "referring_agent": referring_agent}
            except Exception as e:
                c.rollback()
                print(f"[pair_code] insert retry: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[pair_code] get_or_create failed: {e}", file=sys.stderr)
        return None
    finally:
        try: c.close()
        except Exception: pass


def redeem_pair_code(code: str, stripe_session_id: str | None = None) -> dict:
    """Called by the Stripe webhook on checkout.session.completed when
       client_reference_id matches a pair_code. Flips the agent's API
       key from free → developer tier and records the conversion."""
    out = {"ok": False, "code": code}
    if not code:
        out["error"] = "missing_code"
        return out
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, api_key_hash, target_tier, redeemed_at, expires_at
                FROM mcp_pair_codes WHERE code = %s
            """, (code,))
            row = cur.fetchone()
        if not row:
            out["error"] = "code_not_found"
            return out
        pid, key_hash, target_tier, already_redeemed, expires_at = row
        if already_redeemed:
            out["ok"] = True
            out["already_redeemed"] = True
            out["redeemed_at"] = already_redeemed.isoformat()
            return out
        # Mark redeemed first (so a duplicate webhook is a no-op)
        with c.cursor() as cur:
            cur.execute("""
                UPDATE mcp_pair_codes
                SET redeemed_at = NOW(),
                    stripe_session_id = COALESCE(stripe_session_id, %s)
                WHERE id = %s AND redeemed_at IS NULL
                RETURNING id;
            """, (stripe_session_id, pid))
            redeemed_row = cur.fetchone()
        c.commit()
        if not redeemed_row:
            # Race condition — another webhook beat us
            out["ok"] = True
            out["already_redeemed"] = True
            return out
        # Phase FF+8-funnel (2026-05-19) — THE bug that destroyed
        # paid-conversion attribution. The previous code used:
        #
        #   WHERE encode(sha256(key_value::bytea), 'hex') LIKE
        #         _hash_key("") + "%"
        #
        # which compared against the SHA-256 of an EMPTY string, not
        # against this redemption's actual api_key_hash. Result: the
        # UPDATE matched zero rows on every successful checkout, and
        # paid users walked away with no tier flip. The mcp_pair_codes
        # row was marked redeemed (so we LOOKED like we converted),
        # but api_keys.plan stayed 'free'.
        #
        # Fix: match api_keys.key_hash directly against the same
        # _hash_key() output that pair-code creation stored.
        rows_flipped = 0
        try:
            with c.cursor() as cur:
                cur.execute("""
                    UPDATE api_keys
                    SET plan = %s
                    WHERE key_hash = %s
                """, (target_tier, key_hash))
                rows_flipped = cur.rowcount or 0
        except Exception as _flip_err:
            # api_keys table might use a different hash scheme; in that case
            # the operator does a manual upgrade via the existing dashboard.
            # Either way, the conversion is RECORDED so we know to act on it.
            out["flip_error"] = str(_flip_err)[:200]
        c.commit()
        out["rows_flipped"] = rows_flipped
        out["ok"] = True
        out["target_tier"] = target_tier
        out["pair_code_id"] = pid
        return out
    except Exception as e:
        out["error"] = str(e)[:200]
        return out
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@pair_code_bp.post("/api/v1/mcp/pair-code/generate")
def generate_pair_code():
    """Agent calls this when it hits a paywall. Returns a fresh code
       (or reuses an existing one if there's an unredeemed code newer
       than 30 minutes for this api_key)."""
    # Accept api_key from header, body, or query — agents are inconsistent
    api_key = (request.headers.get("X-API-Key")
               or (request.json.get("api_key") if request.is_json else None)
               or request.args.get("api_key") or "")
    if not api_key:
        return jsonify(ok=False, error="api_key_required",
                       hint="POST with X-API-Key header"), 400
    body = request.get_json(silent=True) or {}
    tool_name = body.get("tool_name") or request.args.get("tool")
    market = body.get("market") or request.args.get("market")
    result = get_or_create_code(api_key, tool_name=tool_name, market=market)
    if not result:
        return jsonify(ok=False, error="generation_failed"), 503
    result["ok"] = True
    result["stripe_upgrade_url"] = (
        f"{STRIPE_DEVELOPER_LINK}"
        f"{'&' if '?' in STRIPE_DEVELOPER_LINK else '?'}"
        f"client_reference_id={result['code']}")
    return jsonify(result), 200


@pair_code_bp.get("/api/v1/mcp/pair-code/<code>/status")
def pair_code_status(code):
    """Agent polls this to know when the human completes checkout.
       Returns `redeemed: true` once the Stripe webhook fires."""
    c = _conn()
    if c is None: return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT redeemed_at, expires_at, target_tier,
                       redeem_viewed_at, stripe_clicked_at
                FROM mcp_pair_codes WHERE code = %s
            """, (code.upper(),))
            row = cur.fetchone()
        if not row:
            return jsonify(ok=False, error="code_not_found"), 404
        redeemed_at, expires_at, target_tier, viewed, clicked = row
        now = datetime.now(timezone.utc)
        expired = expires_at and expires_at < now and not redeemed_at
        return jsonify(
            ok=True,
            code=code.upper(),
            redeemed=bool(redeemed_at),
            redeemed_at=redeemed_at.isoformat() if redeemed_at else None,
            expires_at=expires_at.isoformat() if expires_at else None,
            expired=bool(expired),
            target_tier=target_tier,
            # Funnel telemetry — useful for the agent to communicate progress
            redeem_viewed=bool(viewed),
            stripe_clicked=bool(clicked),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@pair_code_bp.get("/redeem/<code>")
def redeem_landing(code):
    """Public user-facing landing page. The human visits this URL after
       their AI agent tells them about the paywall. We show context
       (which agent, which tool, which market) + a one-click upgrade
       CTA that goes straight to Stripe with the pair-code as
       client_reference_id."""
    code = code.upper().strip()
    c = _conn()
    if c is None:
        return Response(_redeem_error_page("Database temporarily unavailable."),
                        mimetype="text/html"), 503
    try:
        # Phase DD+ Play 6: select referring_agent too (column may not
        # exist on fresh DBs that haven't run the migration yet — fall
        # back to None in that case).
        referring_agent = None
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT tool_name, market, target_tier, created_at,
                           expires_at, redeemed_at, referring_agent
                    FROM mcp_pair_codes WHERE code = %s
                """, (code,))
                row = cur.fetchone()
                if row: referring_agent = row[6]
            except Exception:
                c.rollback()
                cur.execute("""
                    SELECT tool_name, market, target_tier, created_at,
                           expires_at, redeemed_at, NULL AS referring_agent
                    FROM mcp_pair_codes WHERE code = %s
                """, (code,))
                row = cur.fetchone()
        if not row:
            return Response(_redeem_error_page(
                f"Code <strong>{_h(code)}</strong> not found. "
                "Pair codes expire after 30 minutes — ask your AI to "
                "generate a new one."), mimetype="text/html"), 404
        tool_name = row[0]
        market = row[1]
        target_tier = row[2]
        expires_at = row[4]
        redeemed_at = row[5]
        if redeemed_at:
            return Response(_redeem_success_page(code, tool_name),
                            mimetype="text/html"), 200
        if expires_at and expires_at < datetime.now(timezone.utc):
            return Response(_redeem_error_page(
                f"Code <strong>{_h(code)}</strong> expired. "
                "Ask your AI agent to generate a new one — they're "
                "valid for 30 minutes."), mimetype="text/html"), 410
        # Record the view
        was_first_view = False
        try:
            with c.cursor() as cur:
                cur.execute("""
                    UPDATE mcp_pair_codes
                    SET redeem_viewed_at = COALESCE(redeem_viewed_at, NOW()),
                        user_agent_at_view = COALESCE(user_agent_at_view, %s)
                    WHERE code = %s
                    RETURNING (redeem_viewed_at::date = CURRENT_DATE
                                AND user_agent_at_view = %s)
                """, ((request.headers.get("User-Agent") or "")[:300], code,
                       (request.headers.get("User-Agent") or "")[:300]))
                r = cur.fetchone()
                was_first_view = bool(r and r[0])
            c.commit()
        except Exception:
            try: c.rollback()
            except Exception: pass

        # Phase QQ (2026-05-15): also record this as an "upgrade_click"
        # signal in mcp_upgrade_signals so the conversion-funnel
        # dashboard has a unified stream. The pair_codes table tracks
        # the URL-view independently; this duplicate row makes
        # cross-funnel queries trivial. Only writes on first view to
        # avoid amplifying every page refresh.
        if was_first_view:
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO mcp_upgrade_signals
                            (signal_type, tool_requested, mcp_client,
                             message_shown, created_at)
                        VALUES ('redeem_url_viewed', %s, %s, %s, NOW())
                    """, (tool_name or 'unknown',
                          (referring_agent or 'unknown')[:200],
                          f"redeem_viewed: code={code}"))
                c.commit()
            except Exception:
                try: c.rollback()
                except Exception: pass
        return Response(_redeem_page(code, tool_name, market, target_tier,
                                       referring_agent),
                        mimetype="text/html"), 200
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Phase FF+7 (2026-05-19) — close the paywall → click → redeem funnel.
#
# L14 (Causal Reasoner) found the actual conversion-leak root cause: the
# MCP paywall response was returning `upgrade_url: "https://dchub.cloud/
# pricing"` — a generic pricing page with NO pair-code, so users had no
# 1-click path back to the specific tool that paywalled them. Redeem
# funnel data confirmed: 15,420 paywall_hits / 30d → 1 click. The leak
# is RIGHT HERE at paywall_hit → click.
#
# Two new entry-points:
#   GET  /upgrade?key=<>&tool=<>&market=<>   — anyone landing here gets
#                                              a pair-code minted and is
#                                              302'd to /redeem/<code>.
#                                              For URL-in-paywall use.
#   POST /api/v1/mcp/paywall-response         — single backend call that
#                                              MCP server can use to get
#                                              a complete paywall payload
#                                              (mints code, records signal,
#                                              returns redeem_url + status
#                                              poll URL). One-trip.
# ---------------------------------------------------------------------------

# Phase FF+8-funnel (2026-05-19) — /upgrade was hanging 8-30s when L8
# Claude background calls held gunicorn workers. Users clicked, got
# a spinner, closed the tab. 200+ high-intent users → 6 conversions.
# Now: in-memory cache + 500ms deadline on the mint. Cache hit is
# instant. Cache miss runs the mint in a thread; if it doesn't return
# in 500ms, we 302 to /pricing with full attribution rather than make
# the user wait. The mint completes in the background and lands in
# the cache for the next click from the same key.
import threading as _threading
import time as _time
_UPGRADE_CODE_CACHE: dict = {}        # api_key_hash -> (code, expires_at_ts)
_UPGRADE_CACHE_LOCK = _threading.Lock()
_UPGRADE_CACHE_TTL_SEC = 1700         # 28 min — just under DB code expiry


def _fast_get_code(api_key, tool, market, agent, deadline_ms=500):
    """Return a pair-code dict in under deadline_ms, or None if the DB
    mint didn't complete fast enough. The mint thread is daemon so it
    can keep running and populate the cache for the next click."""
    h = _hash_key(api_key)
    now = _time.time()
    with _UPGRADE_CACHE_LOCK:
        cached = _UPGRADE_CODE_CACHE.get(h)
        if cached and cached[1] > now:
            return {"code": cached[0], "cached": True}

    box = {"result": None}

    def _mint_in_thread():
        try:
            r = get_or_create_code(api_key, tool_name=tool,
                                   market=market, referring_agent=agent)
            box["result"] = r
            if r and r.get("code"):
                with _UPGRADE_CACHE_LOCK:
                    _UPGRADE_CODE_CACHE[h] = (r["code"],
                                              _time.time() + _UPGRADE_CACHE_TTL_SEC)
        except Exception:
            pass

    t = _threading.Thread(target=_mint_in_thread, daemon=True)
    t.start()
    t.join(timeout=deadline_ms / 1000.0)
    return box["result"]


@pair_code_bp.get("/upgrade")
def upgrade_redirect():
    """Smart-redirect entry point. Mint a pair-code for the caller and
       302 to /redeem/<code>. Falls back to /pricing on any error.

       Query params:
         key    — caller's api_key (also accepted as X-API-Key header)
         tool   — tool name that triggered the paywall (for context on
                  the redeem page)
         market — market name (optional, for context)
         agent  — referring AI agent name (e.g. claude-desktop, cursor)
    """
    from flask import redirect
    api_key = (request.headers.get("X-API-Key")
               or request.args.get("key")
               or request.args.get("api_key") or "")
    tool = request.args.get("tool") or request.args.get("tool_name") or ""
    market = request.args.get("market")
    agent = request.args.get("agent") or request.args.get("referring_agent")

    # Without an api_key we can't mint a code; bounce to /pricing with
    # attribution so we still capture funnel-source even when we can't
    # close the loop.
    if not api_key:
        utm = f"?utm_source=mcp_upgrade&utm_medium=paywall"
        if tool: utm += f"&utm_content={tool}"
        return redirect(f"https://dchub.cloud/pricing{utm}", code=302)

    # Fast path: cached code OR mint completes in <500ms.
    result = _fast_get_code(api_key, tool, market, agent, deadline_ms=500)
    if result and result.get("code"):
        return redirect(f"https://dchub.cloud/redeem/{result['code']}", code=302)

    # Slow path: mint didn't finish in time. Send the user to /pricing
    # with attribution NOW (no more 8-30s spinner). The mint keeps
    # running in the daemon thread and will populate the cache; the
    # next /upgrade click for this key gets the redeem flow.
    return redirect(f"https://dchub.cloud/pricing"
                    f"?utm_source=mcp_upgrade&utm_medium=paywall_fast_fallback"
                    f"&utm_content={tool or 'unknown'}", code=302)


@pair_code_bp.post("/api/v1/mcp/paywall-response")
def paywall_response():
    """One-call paywall response for MCP servers. Mints a pair-code,
       records an upgrade_signal row, returns a structured payload
       the MCP server can ship to the agent verbatim.

       Body (JSON) or query params:
         api_key (required) — caller key (also accepted as X-API-Key)
         tool (required)    — tool that hit the paywall
         market (optional)  — market context for the redeem page
         agent (optional)   — referring AI agent name
         reason (optional)  — short reason string for the agent ("Paid
                              tier required for full result set")

       Returns:
         {
           ok: true,
           reason: "...",
           pair_code: "DCM-4F7K",
           redeem_url: "https://dchub.cloud/redeem/DCM-4F7K",
           upgrade_url: "https://dchub.cloud/upgrade?key=...&tool=...",
           status_poll_url: "https://dchub.cloud/api/v1/mcp/pair-code/<code>/status",
           expires_at: "...",
           message_to_agent: "Tell the human to visit ..." (suggested)
         }

       The MCP server can use either redeem_url (direct deep-link) or
       upgrade_url (generic, lets us re-mint if the user comes back later).
    """
    body = request.get_json(silent=True) or {}
    api_key = (request.headers.get("X-API-Key")
               or body.get("api_key") or request.args.get("api_key") or "")
    tool = body.get("tool") or body.get("tool_name") or request.args.get("tool")
    market = body.get("market") or request.args.get("market")
    agent = body.get("agent") or request.args.get("agent")
    reason = body.get("reason") or "Paid tier required for full result set"

    if not api_key:
        return jsonify(ok=False, error="api_key_required",
                       hint="POST with X-API-Key header or body.api_key"), 400
    if not tool:
        return jsonify(ok=False, error="tool_required",
                       hint="Pass body.tool — the tool that hit the paywall"), 400

    result = get_or_create_code(api_key, tool_name=tool, market=market,
                                referring_agent=agent)
    if not result or not result.get("code"):
        return jsonify(ok=False, error="mint_failed",
                       fallback_url="https://dchub.cloud/pricing"), 503

    code = result["code"]
    # Record paywall_hit upgrade_signal — this is what
    # check_tool_signal_to_conversion_leak watches. Without an explicit
    # row here, the only signals come from the MCP server side, which
    # may not be writing them. Writing from the paywall-response
    # endpoint guarantees per-tool signal counters stay accurate.
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO mcp_upgrade_signals
                        (signal_type, tool_requested, mcp_client,
                         message_shown, created_at)
                    VALUES ('paywall_hit', %s, %s, %s, NOW())
                """, (tool, (agent or "unknown")[:200],
                      f"paywall_hit: code={code}"[:300]))
            c.commit()
        except Exception:
            try: c.rollback()
            except Exception: pass
        try: c.close()
        except Exception: pass

    upgrade_url = (f"https://dchub.cloud/upgrade?key={api_key}"
                   f"&tool={tool}")
    if market: upgrade_url += f"&market={market}"
    if agent:  upgrade_url += f"&agent={agent}"

    return jsonify(
        ok=True,
        reason=reason,
        pair_code=code,
        redeem_url=result["redeem_url"],
        upgrade_url=upgrade_url,
        status_poll_url=f"https://dchub.cloud/api/v1/mcp/pair-code/{code}/status",
        expires_at=result.get("expires_at"),
        message_to_agent=(
            f"This tool requires the Developer tier. Tell the human "
            f"to visit {result['redeem_url']} to upgrade (one click, "
            f"30-min code). I'll poll status and unlock as soon as they "
            f"complete checkout."
        ),
        reused=result.get("reused", False),
    ), 200


@pair_code_bp.post("/api/v1/mcp/pair-code/<code>/clicked")
def pair_code_clicked(code):
    """Called by the redeem page JS when the user clicks the Stripe
       button. Lets us measure stripe_click → conversion drop-off."""
    c = _conn()
    if c is None: return jsonify(ok=True), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE mcp_pair_codes
                SET stripe_clicked_at = COALESCE(stripe_clicked_at, NOW())
                WHERE code = %s
            """, (code.upper(),))
        c.commit()
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True), 200


# ---------------------------------------------------------------------------
# Funnel diagnostics — the OTHER thing the user asked for
# ---------------------------------------------------------------------------

@pair_code_bp.get("/api/v1/mcp/funnel/diagnostics")
def funnel_diagnostics():
    """The drop-off-per-stage view the funnel needs. Currently the
       public /api/v1/mcp/funnel only shows aggregate counters
       (8K signals → 1 conversion). This endpoint shows WHERE
       between those two numbers the leak happens.

       Stages:
         1. signals_30d           — paywall hits (the addressable pool)
         2. pair_codes_generated  — paywall hits that got a code
                                    (some endpoints may not be wired yet)
         3. redeem_viewed         — humans who actually landed on /redeem
         4. stripe_clicked        — humans who clicked Upgrade
         5. redeemed              — Stripe checkout completed +
                                    pair_code marked redeemed
    """
    out = {"as_of": datetime.now(timezone.utc).isoformat(),
           "window_days": 30, "stages": {}}
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return jsonify(out), 503
    try:
        # Existing upgrade-signal count from mcp_upgrade_signals
        try:
            with c.cursor() as cur:
                cur.execute("""SELECT COUNT(*) FROM mcp_upgrade_signals
                               WHERE created_at > NOW() - INTERVAL '30 days'""")
                out["stages"]["1_paywall_signals_30d"] = int((cur.fetchone() or (0,))[0])
        except Exception:
            out["stages"]["1_paywall_signals_30d"] = None
        with c.cursor() as cur:
            cur.execute("""SELECT COUNT(*) FROM mcp_pair_codes
                           WHERE created_at > NOW() - INTERVAL '30 days'""")
            out["stages"]["2_pair_codes_generated_30d"] = int((cur.fetchone() or (0,))[0])
            cur.execute("""SELECT COUNT(*) FROM mcp_pair_codes
                           WHERE redeem_viewed_at IS NOT NULL
                             AND created_at > NOW() - INTERVAL '30 days'""")
            out["stages"]["3_redeem_page_viewed_30d"] = int((cur.fetchone() or (0,))[0])
            cur.execute("""SELECT COUNT(*) FROM mcp_pair_codes
                           WHERE stripe_clicked_at IS NOT NULL
                             AND created_at > NOW() - INTERVAL '30 days'""")
            out["stages"]["4_stripe_clicked_30d"] = int((cur.fetchone() or (0,))[0])
            cur.execute("""SELECT COUNT(*) FROM mcp_pair_codes
                           WHERE redeemed_at IS NOT NULL
                             AND created_at > NOW() - INTERVAL '30 days'""")
            out["stages"]["5_redeemed_30d"] = int((cur.fetchone() or (0,))[0])
        # Convert to a useful "drop-off rate" view
        stages = out["stages"]
        keys = ["1_paywall_signals_30d", "2_pair_codes_generated_30d",
                "3_redeem_page_viewed_30d", "4_stripe_clicked_30d",
                "5_redeemed_30d"]
        out["dropoff_rates"] = {}
        for i in range(len(keys) - 1):
            a = stages.get(keys[i]) or 0
            b = stages.get(keys[i + 1]) or 0
            out["dropoff_rates"][f"{keys[i].split('_')[0]}_to_{keys[i+1].split('_')[0]}"] = (
                round(100.0 * b / a, 2) if a else None)
        resp = jsonify(out)
        resp.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=240"
        return resp, 200
    except Exception as e:
        out["error"] = str(e)[:200]
        return jsonify(out), 500
    finally:
        try: c.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# HTML pages — kept inline to avoid templating dependency
# ---------------------------------------------------------------------------

def _h(s):
    from html import escape
    return escape(str(s or ""))


def _redeem_page(code, tool_name, market, target_tier, referring_agent=None):
    """Phase DD+: now also surfaces:
       - Play 6 affiliate badge ("Upgrading via Claude Desktop")
       - Play 3 one-time top-up alt-action ("not ready? $5 / 50 calls")
       - Play 5 email-trial alt-action ("7-day free Developer trial")
    """
    pretty_tool = (tool_name or "").replace("get_", "").replace("_", " ").title() or "DC Hub paid feature"
    market_line = f"<div class='ctx-row'><span>Market:</span><b>{_h(market)}</b></div>" if market else ""
    stripe_url = (f"{STRIPE_DEVELOPER_LINK}"
                  f"{'&' if '?' in STRIPE_DEVELOPER_LINK else '?'}"
                  f"client_reference_id={_h(code)}")
    # Play 6 — pretty-format the referring agent name
    agent_pretty_map = {
        "claude":     "Claude Desktop",
        "cursor":     "Cursor",
        "gpt":        "ChatGPT / GPT client",
        "openai":     "OpenAI client",
        "gemini":     "Gemini CLI",
        "perplexity": "Perplexity",
        "cline":      "Cline",
        "windsurf":   "Windsurf",
        "copilot":    "GitHub Copilot",
        "grok":       "Grok",
    }
    agent_pretty = agent_pretty_map.get((referring_agent or "").lower(),
                                         referring_agent)
    agent_badge = (
        f"<div style='display:inline-flex;align-items:center;gap:8px;"
        f"background:rgba(99,102,241,0.12);border:1px solid rgba(99,102,241,0.3);"
        f"border-radius:999px;padding:6px 14px;font-size:0.78rem;font-weight:600;"
        f"color:#a8a8f0;margin-bottom:14px'>"
        f"<span style='font-size:0.95rem'>🤖</span>"
        f"Upgrading via <strong style='color:#fff'>{_h(agent_pretty)}</strong></div>"
    ) if agent_pretty else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Unlock your AI agent · DC Hub · Code {_h(code)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0a0a12;--card:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;--green:#10b981;--acc:#6366f1;--gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);}}
*{{box-sizing:border-box}}body{{font-family:Inter,system-ui;background:var(--bg);color:var(--tx);margin:0;line-height:1.55;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}}
.wrap{{max-width:560px;width:100%;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:36px;}}
.kicker{{font-family:JetBrains Mono,monospace;font-size:0.72rem;letter-spacing:0.14em;color:var(--green);text-transform:uppercase;margin-bottom:10px;font-weight:700}}
h1{{font-size:1.6rem;margin:0 0 8px;letter-spacing:-0.02em;font-weight:800}}
.sub{{color:var(--tx2);margin:0 0 24px;font-size:1rem}}
.ctx{{background:rgba(99,102,241,0.06);border:1px solid rgba(99,102,241,0.2);border-radius:10px;padding:16px 18px;margin:0 0 24px;}}
.ctx-row{{display:flex;justify-content:space-between;padding:4px 0;font-size:0.92rem}}
.ctx-row span{{color:var(--tx2)}} .ctx-row b{{color:var(--tx);font-family:JetBrains Mono,monospace}}
.cta{{display:block;background:var(--gradient);color:#fff;text-decoration:none;text-align:center;padding:16px 24px;border-radius:10px;font-weight:700;font-size:1.05rem;letter-spacing:0.01em;transition:transform .1s ease}}
.cta:hover{{transform:translateY(-1px)}}
.cta-sub{{color:var(--tx2);font-size:0.82rem;text-align:center;margin-top:10px}}
.alts{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:18px}}
@media(max-width:480px){{.alts{{grid-template-columns:1fr}}}}
.alt{{padding:12px 14px;border:1px solid var(--bd);border-radius:8px;text-decoration:none;color:var(--tx);background:rgba(255,255,255,0.02);transition:.15s;display:block}}
.alt:hover{{border-color:rgba(99,102,241,0.5)}}
.alt b{{display:block;font-size:0.92rem;margin-bottom:2px}}
.alt span{{color:var(--tx2);font-size:0.78rem}}
.divider{{border:0;border-top:1px solid var(--bd);margin:28px 0 20px}}
.explainer{{color:var(--tx2);font-size:0.88rem;line-height:1.55}}
.steps{{counter-reset:step;list-style:none;padding:0;margin:14px 0 0}}
.steps li{{position:relative;padding:6px 0 6px 32px;color:var(--tx2);font-size:0.9rem}}
.steps li:before{{counter-increment:step;content:counter(step);position:absolute;left:0;top:7px;width:22px;height:22px;border-radius:50%;background:rgba(99,102,241,0.15);border:1px solid var(--acc);color:var(--acc);font-weight:700;font-size:0.78rem;display:flex;align-items:center;justify-content:center}}
.code{{font-family:JetBrains Mono,monospace;background:rgba(255,255,255,0.06);padding:2px 8px;border-radius:4px;color:#fff}}
.trial{{margin-top:14px;padding:14px;background:rgba(16,185,129,0.06);border:1px solid rgba(16,185,129,0.25);border-radius:10px}}
.trial form{{display:flex;gap:8px;margin-top:8px}}
.trial input{{flex:1;background:rgba(255,255,255,0.04);border:1px solid var(--bd);border-radius:6px;color:var(--tx);padding:8px 12px;font-size:0.9rem;font-family:inherit}}
.trial button{{background:var(--green);color:#000;border:none;border-radius:6px;padding:8px 18px;font-weight:700;cursor:pointer;font-size:0.85rem}}
.trial .msg{{font-size:0.78rem;color:var(--tx2);margin-top:6px}}
.foot{{margin-top:24px;text-align:center;color:var(--tx2);font-size:0.78rem}}
.foot a{{color:var(--acc);text-decoration:none}}
</style></head><body>
<div class="wrap">
  <div class="kicker">🔓 AGENT PAIR-LINK</div>
  {agent_badge}
  <h1>Unlock your AI agent</h1>
  <p class="sub">Your AI just hit a paywall and asked you to upgrade. One click and it's back to work — no key swap, no config change.</p>

  <div class="ctx">
    <div class="ctx-row"><span>Pair code:</span><b>{_h(code)}</b></div>
    <div class="ctx-row"><span>Trying to access:</span><b>{_h(pretty_tool)}</b></div>
    {market_line}
    <div class="ctx-row"><span>Plan:</span><b>Developer · $49/mo</b></div>
  </div>

  <a href="{stripe_url}" class="cta" id="cta"
     onclick="navigator.sendBeacon('/api/v1/mcp/pair-code/{_h(code)}/clicked')">
    Unlock for $49/mo →
  </a>
  <div class="cta-sub">Secure checkout via Stripe · 7-day money-back</div>

  <!-- Play 3 + Play 5 alternative paths -->
  <div class="alts">
    <a href="javascript:void(0)" class="alt" id="topup-link">
      <b>💸 One-time top-up</b>
      <span>50 extra calls for $5 · no subscription</span>
    </a>
    <a href="javascript:void(0)" class="alt" id="trial-toggle">
      <b>🎯 Try 7 days free</b>
      <span>Full Developer access · email magic link</span>
    </a>
  </div>

  <div class="trial" id="trial-form" style="display:none">
    <strong style="color:#fff;font-size:0.9rem">Get 7 days of full Developer access</strong>
    <div style="font-size:0.78rem;color:var(--tx2);margin-top:4px">
      No credit card. We'll send a one-click activation link.
    </div>
    <form id="trial-form-el" onsubmit="return submitTrial(event)">
      <input type="text" name="website" style="display:none" tabindex="-1" autocomplete="off">
      <input type="email" id="trial-email" placeholder="you@company.com" required>
      <button type="submit">Send link →</button>
    </form>
    <div class="msg" id="trial-msg"></div>
  </div>

  <hr class="divider">
  <div class="explainer">
    <strong style="color:#fff">How it works:</strong>
    <ol class="steps">
      <li>Click <b>Unlock</b> above, complete Stripe checkout.</li>
      <li>Your AI agent's API key (the one already in its config) gets promoted instantly.</li>
      <li>Tell your agent "try again" — its next call returns the data it was blocked on.</li>
    </ol>
    <p style="margin-top:18px;font-size:0.82rem;color:var(--tx2)">
      Code expires 30 minutes after generation. <span class="code">{_h(code)}</span>
      identifies which API key gets upgraded — keep this URL private.
    </p>
  </div>

  <div class="foot">
    <a href="/pricing">Compare plans</a> · <a href="/dcpi">Open DCPI</a> · <a href="/mcp">MCP docs</a>
  </div>
</div>
<script>
  // Top-up flow: generate a token for this api_key via API, then redirect
  // to /topup/{{token}} which has the Stripe button.
  document.getElementById('topup-link').addEventListener('click', async () => {{
    try {{
      // We don't have the api_key on the client. Best UX: route to /pricing#topup
      // which has the top-up explainer + manual instructions for now.
      window.location.href = '/pricing#topup';
    }} catch (e) {{ /* silent */ }}
  }});
  // Trial form toggle
  document.getElementById('trial-toggle').addEventListener('click', () => {{
    const f = document.getElementById('trial-form');
    f.style.display = f.style.display === 'none' ? 'block' : 'none';
    document.getElementById('trial-email')?.focus();
  }});
  async function submitTrial(ev) {{
    ev.preventDefault();
    const email = document.getElementById('trial-email').value;
    const msg = document.getElementById('trial-msg');
    msg.textContent = 'Sending…';
    try {{
      const r = await fetch('/api/v1/trial/start', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{email, source: 'redeem_page',
                              referring_agent: {json.dumps(referring_agent or "")}}}),
      }});
      const d = await r.json();
      if (d.ok) {{
        msg.style.color = '#10b981';
        msg.textContent = '✓ Check your inbox for the activation link (5 min).';
      }} else {{
        msg.style.color = '#f59e0b';
        msg.textContent = 'Could not send: ' + (d.error || 'try again');
      }}
    }} catch (e) {{
      msg.style.color = '#f59e0b';
      msg.textContent = 'Network error — try again.';
    }}
    return false;
  }}
</script>
</body></html>"""


def _redeem_success_page(code, tool_name):
    pretty = (tool_name or "").replace("get_", "").replace("_", " ").title() or "your AI agent"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Unlocked · {_h(code)}</title>
<style>body{{font-family:system-ui;background:#0a0a12;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px}}
.wrap{{max-width:520px;text-align:center;background:#11121a;border:1px solid #1f2030;border-radius:14px;padding:40px}}
h1{{font-size:1.6rem;color:#10b981;margin:0 0 10px}}p{{color:#9ca3af}}a{{color:#6366f1}}</style></head>
<body><div class="wrap">
<div style="font-size:3rem;margin-bottom:8px">✓</div>
<h1>Unlocked</h1>
<p>This pair code has already been redeemed. Your AI agent's API key is now on the Developer tier — tell it to retry <b>{_h(pretty)}</b> and it'll get the full data.</p>
<p style="margin-top:24px;font-size:0.85rem">
<a href="/dashboard">Open dashboard</a> · <a href="/dcpi">Open DCPI</a></p>
</div></body></html>"""


def _redeem_error_page(message_html):
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Pair code · DC Hub</title>
<style>body{{font-family:system-ui;background:#0a0a12;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px}}
.wrap{{max-width:520px;text-align:center;background:#11121a;border:1px solid #1f2030;border-radius:14px;padding:40px}}
h1{{font-size:1.4rem;margin:0 0 10px}}p{{color:#9ca3af;line-height:1.55}}a{{color:#6366f1}}
strong{{color:#f59e0b;font-family:JetBrains Mono,monospace}}</style></head>
<body><div class="wrap">
<div style="font-size:3rem;margin-bottom:8px;color:#f59e0b">⚠</div>
<h1>Pair code unavailable</h1>
<p>{message_html}</p>
<p style="margin-top:20px;font-size:0.85rem">
<a href="/pricing">Compare plans</a> · <a href="/dcpi">Open DCPI</a></p>
</div></body></html>"""
